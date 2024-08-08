import argparse
import json
import logging
import signal
import sqlite3
import time
import traceback
import uuid
import pytz
import subprocess
import bittensor as bt
import rich
import prompt_toolkit
from prompt_toolkit.shortcuts import clear
from rich.console import Console
from rich.table import Table
from prompt_toolkit.application import Application as PTApplication
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Layout
from prompt_toolkit.widgets import Frame, TextArea, Label
from prompt_toolkit.styles import Style
from prompt_toolkit.layout.containers import Window, HSplit
from bettensor.miner.database.database_manager import DatabaseManager
from bettensor.miner.database.predictions import PredictionsHandler
from bettensor.miner.database.games import GamesHandler
from bettensor.miner.stats.miner_stats import MinerStateManager, MinerStatsHandler
import threading
import os
import sys
import subprocess
import atexit
from prompt_toolkit.output import Output
from datetime import datetime, timezone

# Create logs directory if it doesn't exist
log_dir = "./logs"
if not os.path.exists(log_dir):
    os.makedirs(log_dir)

# Set up logging for CLI
cli_log_file = os.path.join(log_dir, f"cli_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(cli_log_file),
        logging.StreamHandler(sys.stderr)  # Log to stderr instead of stdout to avoid interfering with CLI output
    ]
)

# Create a logger for the CLI
cli_logger = logging.getLogger("cli")

global_style = Style.from_dict({
    "text-area": "fg:green",
    "frame": "fg:green",
    "label": "fg:green",
    "wager-input": "fg:green",
})

def safe_format(value, format_str):
    try:
        return format_str.format(value)
    except (ValueError, TypeError):
        return str(value)

class Application:
    def __init__(self):
        """
        Initialize the CLI Application.

        Behavior:
            - Sets up the database connection
            - Retrieves available miners
            - Selects a miner to work with
            - Loads miner data and initializes the UI
        """
        # Create a unique channel for this CLI instance
        self.cli_channel = f'cli:{uuid.uuid4()}'

        # Initialize database manager
        db_host = os.getenv('DB_HOST', 'localhost')
        db_name = os.getenv('DB_NAME', 'bettensor')
        db_user = os.getenv('DB_USER', 'root')
        db_password = os.getenv('DB_PASSWORD', 'bettensor_password')
        self.db_manager = DatabaseManager(db_name, db_user, db_password, db_host)
        
        # Set db_path for compatibility with existing code
        self.db_manager.db_path = os.getenv('DB_PATH', os.path.expanduser("~/bettensor/data/miner.db"))
        
        if not os.path.exists(self.db_manager.db_path):
            raise ValueError("Error: Database not found. Please start the miner first.")

        # Parse command-line arguments
        parser = argparse.ArgumentParser(description="BetTensor Miner CLI")
        parser.add_argument("--uid", help="Specify the miner UID to start with")
        args = parser.parse_args()

        # Query for available miners using miner_stats table
        self.available_miners = self.get_available_miners()

        bt.logging.info(f"Available miners: {self.available_miners}")

        if not self.available_miners:
            bt.logging.error("No miners found in the database. Please start the miner first.")
            print("Error: No miners found in the database. Please start the miner first.")
            sys.exit(1)

        self.miner_stats = {str(row['miner_uid']): {'miner_hotkey': row['miner_hotkey'], 'miner_cash': row['miner_cash'], 'miner_rank': row['miner_rank']} for row in self.available_miners}

        # Load the saved miner UID or use the one specified in the command-line argument
        self.current_miner_uid = args.uid if args.uid else self.get_saved_miner_uid()
        bt.logging.info(f"Loaded miner UID: {self.current_miner_uid}")

        # Try to find a valid miner
        valid_miner_found = False
        for miner in self.available_miners:
            if str(miner['miner_uid']) == str(self.current_miner_uid):
                self.miner_hotkey = str(miner['miner_hotkey'])
                self.miner_uid = str(miner['miner_uid'])
                valid_miner_found = True
                break

        if not valid_miner_found:
            bt.logging.warning(f"Miner with UID {self.current_miner_uid} not found. Using the first available miner.")
            self.miner_hotkey = str(self.available_miners[0]['miner_hotkey'])
            self.miner_uid = str(self.available_miners[0]['miner_uid'])

        bt.logging.info(f"Selected miner Hotkey: {self.miner_hotkey}, UID: {self.miner_uid}")

        # Save the current miner UID
        self.save_miner_uid(self.miner_uid)

        # Create an instance of MinerStateManager
        self.state_manager = MinerStateManager(self.db_manager, self.miner_hotkey, self.miner_uid, None)

        # Create an instance of MinerStatsHandler
        self.stats_handler = MinerStatsHandler(self.db_manager, self.state_manager)

        # Set the stats_handler in state_manager
        self.state_manager.stats_handler = self.stats_handler

        self.state_manager.load_state()  # This will recalculate the miner's cash
        
        self.predictions_handler = PredictionsHandler(self.db_manager, self.state_manager, self.miner_hotkey)
        self.games_handler = GamesHandler(self.db_manager, self.predictions_handler)

        # Initialize unsubmitted_predictions
        self.unsubmitted_predictions = {}

        bt.logging.info("Initializing Application")
        bt.logging.info(f"Loaded miner stats: {self.miner_stats}")

        self.reload_data()
        self.running = True
        self.bindings = self.setup_key_bindings()
        self.layout = self.setup_layout()
        self.app = PTApplication(
            layout=self.layout,
            key_bindings=self.bindings,
            full_screen=True,
            style=global_style,
        )
        self.app.custom_app = self
        atexit.register(self.cleanup)

    def setup_key_bindings(self):
        kb = KeyBindings()

        @kb.add('c-c')
        @kb.add('c-q')
        def _(event):
            self.quit()

        @kb.add('up')
        def _(event):
            if hasattr(self, 'current_view'):
                self.current_view.move_up()

        @kb.add('down')
        def _(event):
            if hasattr(self, 'current_view'):
                self.current_view.move_down()

        @kb.add('enter')
        def _(event):
            if hasattr(self, 'current_view'):
                self.current_view.handle_enter()

        @kb.add('left')
        def _(event):
            if isinstance(self.current_view, (PredictionsList, GamesList)):
                self.current_view.move_left()

        @kb.add('right')
        def _(event):
            if isinstance(self.current_view, (PredictionsList, GamesList)):
                self.current_view.move_right()

        return kb

    def setup_layout(self):
        self.current_view = MainMenu(self)
        return Layout(self.current_view.box)

    def cleanup(self):
        if self.app and self.app.output:
            self.app.output.reset_attributes()
            self.app.output.enable_autowrap()
            self.app.output.quit_alternate_screen()
            self.app.output.flush()
        os.system('reset')

    def quit(self):
        cli_logger.info("Initiating shutdown...")
        self.running = False
        try:
            self.state_manager.save_state()  # Save state before exiting
            cli_logger.info(f"Final miner cash: {self.state_manager.get_stats()['miner_cash']}")
        except Exception as e:
            cli_logger.error(f"Error during shutdown: {e}")
            cli_logger.error(traceback.format_exc())
        finally:
            try:
                if self.app.is_running:
                    self.app.exit()
            except Exception as e:
                cli_logger.error(f"Error during application exit: {e}")
                cli_logger.error(traceback.format_exc())

        # Ensure terminal is reset
        self.cleanup()

        # Restart the application if we're selecting a new miner
        if self.current_view and isinstance(self.current_view, MainMenu) and self.current_view.selected_index == 2:
            python = sys.executable
            os.execl(python, python, *sys.argv)
        else:
            # If not restarting, exit explicitly
            sys.exit(0)

    def run(self):
        def run_app():
            try:
                self.app.run()
            except Exception as e:
                cli_logger.error(f"Error in app: {e}")
                cli_logger.error(traceback.format_exc())
            finally:
                self.running = False

        app_thread = threading.Thread(target=run_app, daemon=True)
        app_thread.start()

        try:
            while self.running:
                if not app_thread.is_alive():
                    break
                app_thread.join(0.1)
        except KeyboardInterrupt:
            cli_logger.info("Keyboard interrupt received. Shutting down...")
        finally:
            self.quit()

    def get_available_miners(self):
        """
        Retrieve all available miners from the database.

        Returns:
            List[Tuple[str, str]]: A list of tuples containing miner UIDs and hotkeys.

        Behavior:
            - Queries the database for all miner UIDs and hotkeys
        """
        query = "SELECT miner_uid, miner_hotkey, miner_cash, miner_rank FROM miner_stats"
        try:
            result = self.db_manager.execute_query(query)
            bt.logging.info(f"Retrieved miners: {result}")
            return result
        except Exception as e:
            bt.logging.error(f"Failed to retrieve miners: {e}")
            return []

    def select_next_miner(self):
        """
        Rotate to the next available miner and restart the application.

        Behavior:
            - Cycles through available miners
            - Saves the new miner UID
            - Restarts the entire application
        """
        current_index = next((i for i, miner in enumerate(self.available_miners) if str(miner['miner_uid']) == str(self.miner_uid)), -1)
        next_index = (current_index + 1) % len(self.available_miners)
        next_miner_uid = str(self.available_miners[next_index]['miner_uid'])
        
        # Save the next miner UID to a file
        self.save_miner_uid(next_miner_uid)
        
        # Quit the application, which will trigger a restart
        self.quit()

    @staticmethod
    def get_saved_miner_uid():
        """
        Retrieve the saved miner UID from file.
        If the file doesn't exist, return None.
        """
        file_path = 'current_miner_uid.txt'
        try:
            with open(file_path, 'r') as f:
                return f.read().strip()
        except FileNotFoundError:
            bt.logging.warning(f"{file_path} not found. Will use the first available miner.")
            return None

    @staticmethod
    def save_miner_uid(uid):
        """
        Save the current miner UID to a file.
        """
        file_path = 'current_miner_uid.txt'
        with open(file_path, 'w') as f:
            f.write(str(uid))
        bt.logging.info(f"Saved miner UID {uid} to {file_path}")

    def reload_data(self):
        """
        Reload all data for the current miner.

        Behavior:
            - Reloads games and predictions from the database
            - Processes recent predictions
            - Updates the miner's stats
        """
        try:
            self.games = self.games_handler.get_active_games()
            self.predictions = self.predictions_handler.get_predictions(self.miner_hotkey)
            self.predictions_with_teams = self.predictions_handler.get_predictions_with_teams(self.miner_hotkey)
            self.reload_miner_stats()
        except Exception as e:
            cli_logger.error(f"Error reloading data: {str(e)}")
            self.games = {}
            self.predictions = {}
            self.predictions_with_teams = {}

    def change_view(self, new_view):
        """
        Change the current view of the CLI.

        Args:
            new_view: The new view to display.

        Behavior:
            - Updates the current_view attribute
            - Changes the layout container to the new view
        """
        self.reload_miner_stats()  # Reload stats before changing view
        self.current_view = new_view
        self.layout.container = new_view.box
        self.app.invalidate()

        # If changing to MainMenu, update miner data
        if isinstance(new_view, MainMenu):
            new_view.update_text_area()

    def check_db_init(self):
        """
        Check if the database is properly initialized.

        Behavior:
            - Attempts to query the predictions table
            - If an exception occurs, prints an error message
        """
        try:
            query = "SELECT * FROM predictions LIMIT 1"
            self.db_manager.execute_query(query)
        except Exception as e:
            raise ValueError(f"Database not initialized properly, restart your miner first: {e}")

    def check_unsubmitted_predictions(self):
        pass

    def submit_predictions(self):
        self.check_unsubmitted_predictions()
        for prediction_id, prediction in self.unsubmitted_predictions.items():
            try:
                self.predictions_handler.add_prediction(prediction)
                self.reload_miner_stats()  # Reload stats after each prediction
            except Exception as e:
                cli_logger.error(f"Failed to submit prediction {prediction_id}: {str(e)}")
        self.check_unsubmitted_predictions()
        self.unsubmitted_predictions.clear()
        self.check_unsubmitted_predictions()
        self.reload_miner_stats()  # Reload stats after all predictions are submitted

    def get_predictions(self):
        """
        Retrieve all predictions from the database for the current miner.

        Returns:
            Dict[str, Dict]: A dictionary of predictions, keyed by prediction ID.

        Behavior:
            - Queries the database for all predictions for the current miner
            - Constructs a dictionary of prediction data
        """
        return self.predictions_handler.get_predictions(self.miner_hotkey)

    def get_game_data(self):
        """
        Retrieve all inactive game data from the database.

        Returns:
            Dict[str, Dict]: A dictionary of game data, keyed by game ID.

        Behavior:
            - Queries the database for all inactive games
            - Constructs a dictionary of game data
        """
        return self.games_handler.get_active_games()

    def get_miner_stats(self, miner_uid):
        """
        Retrieve stats for the current miner.

        Returns:
            Dict: A dictionary of miner stats.

        Behavior:
            - Retrieves the current miner stats from the state manager
        """
        bt.logging.info(f"Getting miner stats for UID: {miner_uid}")
        stats = self.state_manager.get_stats()
        bt.logging.info(f"Retrieved miner stats: {stats}")
        return stats

    def update_miner_stats(self, wager, prediction_date):
        self.state_manager.update_on_prediction({'wager': wager, 'predictionDate': prediction_date})
        self.reload_data()

    def reload_miner_stats(self):
        self.state_manager.reconcile_state()  # This will handle daily resets and recalculations
        self.miner_stats = self.state_manager.get_stats()
        self.miner_cash = self.miner_stats["miner_cash"]

    def add_prediction(self, prediction):
        prediction_id = str(uuid.uuid4())
        prediction['predictionID'] = prediction_id
        self.predictions[prediction_id] = prediction
        self.predictions_handler.add_prediction(prediction)
        self.update_miner_cash(-prediction['wager'])

    def update_miner_cash(self, amount):
        self.miner_cash += amount
        self.state_manager.update_on_prediction({'wager': amount, 'predictionDate': datetime.now(timezone.utc).isoformat()})
        self.reload_miner_stats()


class InteractiveTable:
    """
    Base class for interactive tables
    """

    def __init__(self, app):
        """
        Initialize the InteractiveTable.

        Args:
            app: The main Application instance.

        Behavior:
            - Sets up the text area for displaying options
            - Initializes the frame and box for layout
            - Sets up the initial selected index and options list
        """
        self.app = app
        self.text_area = TextArea(
            focusable=True,
            read_only=True,
            width=prompt_toolkit.layout.Dimension(preferred=70),
            height=prompt_toolkit.layout.Dimension(
                preferred=20
            ),
        )
        self.frame = Frame(self.text_area, style="class:frame")
        self.box = HSplit([self.frame])
        self.selected_index = 0
        self.options = []

    def update_text_area(self):
        """
        Update the text area with the current options and selection.

        Behavior:
            - Formats the options list with the current selection highlighted
            - Updates the text area content
        """
        lines = [
            f"> {option}" if i == self.selected_index else f"  {option}"
            for i, option in enumerate(self.options)
        ]
        self.text_area.text = "\n".join(lines)

    def handle_enter(self):
        """
        Handle the enter key press.

        Behavior:
            - If the "Go Back" option is selected, changes the view to the main menu
            - Otherwise, does nothing (for now)
        """
        if self.selected_index == len(self.options) - 1:  # Go Back
            self.app.change_view(MainMenu(self.app))
        
        

    def move_up(self):
        """
        Move the selection up.

        Behavior:
            - Decrements the selected index if not at the top
            - Updates the text area
        """
        if self.selected_index > 0:
            self.selected_index -= 1
        self.update_text_area()

    def move_down(self):
        """
        Move the selection down.

        Behavior:
            - Increments the selected index if not at the bottom
            - Updates the text area
        """
        if self.selected_index < len(self.options) - 1:
            self.selected_index += 1
        self.update_text_area()


class MainMenu(InteractiveTable):
    """
    Main menu for the miner CLI - 1st level menu
    """

    def __init__(self, app):
        """
        Initialize the MainMenu.

        Args:
            app: The main Application instance.

        Behavior:
            - Sets up the header and options for the main menu
            - Calls the parent class initializer
            - Updates the text area with initial content
        """
        super().__init__(app)
        app.reload_miner_stats()  # Reload stats when initializing MainMenu

        self.header = Label(
            " BetTensor Miner Main Menu", style="bold"
        )
        self.options = [
            "View Submitted Predictions",
            "View Games and Make Predictions",
            "Select Next Miner (This will trigger app restart, please be patient)",
            "Quit",
        ]
        self.update_text_area()

    def update_text_area(self):
        """
        Update the text area with the current miner stats and menu options.

        Behavior:
            - Formats the miner stats and menu options
            - Updates the text area content
            - Handles potential None values in miner stats
        """
        self.app.reload_miner_stats()  # Reload stats before updating text area
        header_text = self.header.text
        divider = "-" * len(header_text)

        # Helper function to safely format miner stat values
        def safe_format(key, format_spec=None):
            value = self.app.miner_stats.get(key, 'N/A')
            if value is None:
                return 'N/A'
            if format_spec:
                return format_spec.format(value)
            return str(value)

        # Helper function to format the last prediction date
        def format_last_prediction_date(date_str):
            if date_str is None:
                return "N/A"
            try:
                date = datetime.fromisoformat(date_str)
                return date.strftime("%Y-%m-%d %H:%M")
            except ValueError:
                return str(date_str)

        # Miner stats
        miner_stats_text = (
            f" Miner Hotkey: {safe_format('miner_hotkey')}\n"
            f" Miner UID: {safe_format('miner_uid')}\n"
            f" Miner Rank: {safe_format('miner_rank')}\n"
            f" Miner Cash: {safe_format('miner_cash', '{:.2f}')}\n"
            f" Current Incentive: {safe_format('miner_current_incentive', '{:.2f}')} τ per day\n"
            f" Last Prediction: {format_last_prediction_date(self.app.miner_stats.get('miner_last_prediction_date'))}\n"
            f" Lifetime Earnings: ${safe_format('miner_lifetime_earnings', '{:.2f}')}\n"
            f" Lifetime Wager Amount: {safe_format('miner_lifetime_wager', '{:.2f}')}\n"
            f" Lifetime Wins: {safe_format('miner_lifetime_wins')}\n"
            f" Lifetime Losses: {safe_format('miner_lifetime_losses')}\n"
            f" Win/Loss Ratio: {safe_format('miner_win_loss_ratio', '{:.2f}')}\n"
        )

        options_text = "\n".join(
            f"> {option}" if i == self.selected_index else f"  {option}"
            for i, option in enumerate(self.options)
        )

        self.text_area.text = (
            f"{header_text}\n{divider}\n{miner_stats_text}\n{divider}\n{options_text}"
        )

    def handle_enter(self):
        """
        Handle the enter key press in the main menu.

        Behavior:
            - Performs the action corresponding to the selected option
            - Changes view or exits the application based on the selection
        """
        if self.selected_index == 0:
            self.app.change_view(PredictionsList(self.app))
        elif self.selected_index == 1:
            self.app.change_view(GamesList(self.app))
        elif self.selected_index == 2:
            self.show_loading_message()
            self.app.select_next_miner()
        elif self.selected_index == 3:
            self.app.quit()

    def show_loading_message(self):
        self.text_area.text = "Loading next miner... Please wait."
        self.app.app.invalidate()

    def move_up(self):
        """
        Move the selection up in the main menu.

        Behavior:
            - Decrements the selected index if not at the top
            - Updates the text area
        """
        super().move_up()
        self.update_text_area()

    def move_down(self):
        """
        Move the selection down in the main menu.

        Behavior:
            - Increments the selected index if not at the bottom
            - Updates the text area
        """
        super().move_down()
        self.update_text_area()

class PredictionsList(InteractiveTable):
    def __init__(self, app):
        super().__init__(app)
        app.reload_miner_stats()
        app.reload_data()
        self.message = ""
        self.predictions_per_page = 25
        self.current_page = 0
        self.update_sorted_predictions()
        self.update_total_pages()
        self.update_options()
        self.selected_index = len(self.options)  # Set cursor to "Go Back"
        self.header = "Predictions List"  
        self.update_text_area()

    def update_sorted_predictions(self):
        self.sorted_predictions = sorted(
            self.app.predictions_with_teams.values(),
            key=lambda x: x.get("predictionDate") or "",
            reverse=True
        ) if self.app.predictions_with_teams else []

    def update_total_pages(self):
        self.total_pages = max(1, (len(self.sorted_predictions) + self.predictions_per_page - 1) // self.predictions_per_page)
        self.current_page = min(self.current_page, self.total_pages - 1)

    def update_options(self):
        header_lengths = {
            'Date': len('Date'),
            'Home': len('Home'),
            'Away': len('Away'),
            'Predicted Outcome': len('Predicted Outcome'),
            'Wager': len('Wager'),
            'Wager Odds': len('Wager Odds'),
            'Result': len('Result')
        }

        max_lengths = {
            'Date': max(header_lengths['Date'], *(len(self.format_date(pred['predictionDate'])) for pred in self.sorted_predictions)),
            'Home': max(header_lengths['Home'], *(len(pred['Home']) for pred in self.sorted_predictions)),
            'Away': max(header_lengths['Away'], *(len(pred['Away']) for pred in self.sorted_predictions)),
            'Predicted Outcome': max(header_lengths['Predicted Outcome'], *(len(pred['predictedOutcome']) for pred in self.sorted_predictions)),
            'Wager': max(header_lengths['Wager'], *(len(safe_format(pred['wager'], "${:.2f}")) for pred in self.sorted_predictions)),
            'Wager Odds': max(header_lengths['Wager Odds'], *(len(safe_format(pred['wagerOdds'], "{:.2f}")) for pred in self.sorted_predictions)),
            'Result': max(header_lengths['Result'], *(len(pred['outcome']) for pred in self.sorted_predictions))
        }

        self.options = [
            f"{self.format_date(pred['predictionDate']):<{max_lengths['Date']}} | "
            f"{pred['Home']:<{max_lengths['Home']}} | "
            f"{pred['Away']:<{max_lengths['Away']}} | "
            f"{pred['predictedOutcome']:<{max_lengths['Predicted Outcome']}} | "
            f"{safe_format(pred['wager'], '${:.2f}'):<{max_lengths['Wager']}} | "
            f"{safe_format(pred['wagerOdds'], '{:.2f}'):<{max_lengths['Wager Odds']}} | "
            f"{pred['outcome']:<{max_lengths['Result']}}"
            for pred in self.sorted_predictions[self.current_page * self.predictions_per_page:(self.current_page + 1) * self.predictions_per_page]
        ]
        self.options.append("Go Back")

    def update_text_area(self):
        self.app.reload_miner_stats()
        header_text = self.header
        if not self.sorted_predictions:
            self.text_area.text = f"{header_text}\n\nNo predictions available. Go to 'View Games and Make Predictions' to submit a prediction.\n\n> Go Back"
            return

        header_lengths = {
            'Date': len('Date'),
            'Home': len('Home'),
            'Away': len('Away'),
            'Predicted Outcome': len('Predicted Outcome'),
            'Wager': len('Wager'),
            'Wager Odds': len('Wager Odds'),
            'Result': len('Result')
        }

        max_lengths = {
            'Date': max(header_lengths['Date'], *(len(self.format_date(pred['predictionDate'])) for pred in self.sorted_predictions)),
            'Home': max(header_lengths['Home'], *(len(pred['Home']) for pred in self.sorted_predictions)),
            'Away': max(header_lengths['Away'], *(len(pred['Away']) for pred in self.sorted_predictions)),
            'Predicted Outcome': max(header_lengths['Predicted Outcome'], *(len(pred['predictedOutcome']) for pred in self.sorted_predictions)),
            'Wager': max(header_lengths['Wager'], *(len(safe_format(pred['wager'], "${:.2f}")) for pred in self.sorted_predictions)),
            'Wager Odds': max(header_lengths['Wager Odds'], *(len(safe_format(pred['wagerOdds'], "{:.2f}")) for pred in self.sorted_predictions)),
            'Result': max(header_lengths['Result'], *(len(pred['outcome']) for pred in self.sorted_predictions))
        }

        header_row = f"  {'Date':<{max_lengths['Date']}} | {'Home':<{max_lengths['Home']}} | {'Away':<{max_lengths['Away']}} | {'Predicted Outcome':<{max_lengths['Predicted Outcome']}} | {'Wager':<{max_lengths['Wager']}} | {'Wager Odds':<{max_lengths['Wager Odds']}} | {'Result':<{max_lengths['Result']}}"
        divider = "-" * len(header_row)
        options_text = "\n".join(
            f"{'>' if i == self.selected_index else ' '} {option}"
            for i, option in enumerate(self.options)
        )
        page_info = f"\nPage {self.current_page + 1}/{self.total_pages} (Use left/right arrow keys to navigate)"
        self.text_area.text = f"{header_text}\n{divider}\n{header_row}\n{divider}\n{options_text}{page_info}\n\n{self.message}"

    def handle_enter(self):
        if self.selected_index == len(self.options) - 1:  # Go Back
            self.app.change_view(MainMenu(self.app))
        else:
            # Handle other options if needed
            pass

    def move_left(self):
        if self.current_page > 0:
            self.current_page -= 1
            self.update_options()
            self.selected_index = min(self.selected_index, len(self.options) - 1)
            self.update_text_area()

    def move_right(self):
        if self.current_page < self.total_pages - 1:
            self.current_page += 1
            self.update_options()
            self.selected_index = min(self.selected_index, len(self.options) - 1)
            self.update_text_area()

    def format_date(self, date_str):
        if date_str is None:
            return "N/A"
        try:
            date = datetime.fromisoformat(date_str)
            return date.strftime("%Y-%m-%d %H:%M")
        except ValueError:
            return str(date_str)



class GamesList(InteractiveTable):
    def __init__(self, app):
        """
        Initialize the GamesList.

        Args:
            app: The main Application instance.

        Behavior:
            - Sets up the games list view
            - Loads and sorts games
            - Initializes pagination and filtering
        """
        super().__init__(app)
        app.reload_miner_stats()  # Reload stats when initializing GamesList
        app.reload_data()
        self.available_sports = sorted(set(game.sport for game in self.app.games.values()))
        self.current_filter = "All Sports"
        self.update_sorted_games()
        self.games_per_page = 25
        self.current_page = 0
        self.update_total_pages()
        self.update_options()
        self.update_text_area()

    def update_options(self):
        """
        Update the options list for the games view.

        Behavior:
            - Calculates dynamic column widths based on data
            - Formats games data with proper alignment and separators
            - Handles the case of no available games
        """
        if not self.sorted_games:
            self.options = ["No games available", f"Filter: {self.current_filter}", "Go Back"]
            self.header = "No games available"
            return

        start_idx = self.current_page * self.games_per_page
        end_idx = min(start_idx + self.games_per_page, len(self.sorted_games))
        
        # Calculate maximum widths for each column
        max_sport_len = max(len("Sport"), max(len(game.sport) for game in self.sorted_games))
        max_teamA_len = max(len("Team A"), max(len(game.teamA) for game in self.sorted_games))
        max_teamB_len = max(len("Team B"), max(len(game.teamB) for game in self.sorted_games))
        max_eventStartDate_len = max(len("Event Start Date"), max(len(self.format_event_start_date(game.eventStartDate)) for game in self.sorted_games))
        max_teamAodds_len = max(len("Team A Odds"), max(len(self.format_odds(game.teamAodds)) for game in self.sorted_games))
        max_teamBodds_len = max(len("Team B Odds"), max(len(self.format_odds(game.teamBodds)) for game in self.sorted_games))
        max_tieOdds_len = max(len("Tie Odds"), max(len(self.format_odds(game.tieOdds)) for game in self.sorted_games))

        # Define the header with calculated widths, adding a space at the beginning for cursor alignment
        self.header = (
            f"  {'Sport':<{max_sport_len}} | "
            f"{'Team A':<{max_teamA_len}} | "
            f"{'Team B':<{max_teamB_len}} | "
            f"{'Event Start Date':<{max_eventStartDate_len}} | "
            f"{'Team A Odds':<{max_teamAodds_len}} | "
            f"{'Team B Odds':<{max_teamBodds_len}} | "
            f"{'Tie Odds':<{max_tieOdds_len}}"
        )

        # Generate options for the current page
        self.options = []
        for game in self.sorted_games[start_idx:end_idx]:
            self.options.append(
                f"{game.sport:<{max_sport_len}} | "
                f"{game.teamA:<{max_teamA_len}} | "
                f"{game.teamB:<{max_teamB_len}} | "
                f"{self.format_event_start_date(game.eventStartDate):<{max_eventStartDate_len}} | "
                f"{self.format_odds(game.teamAodds):<{max_teamAodds_len}} | "
                f"{self.format_odds(game.teamBodds):<{max_teamBodds_len}} | "
                f"{self.format_odds(game.tieOdds):<{max_tieOdds_len}}"
            )
        self.options.append(f"Filter: {self.current_filter}")
        self.options.append("Go Back")

    def format_odds(self, value):
        """
        Format odds values, handling both string and float types.

        Args:
            value: The value to format (can be string or float).

        Returns:
            str: The formatted value as a string.
        """
        if isinstance(value, str):
            return value
        elif isinstance(value, (int, float)):
            return f"{value:.2f}"
        else:
            return str(value)

    def update_sorted_games(self):
        current_time = datetime.now(timezone.utc)
        self.sorted_games = sorted(
            [game for game in self.app.games.values() if self.parse_date(game.eventStartDate) > current_time],
            key=lambda x: self.parse_date(x.eventStartDate)
        )

    @staticmethod
    def parse_date(date_string):
        try:
            dt = datetime.fromisoformat(date_string.replace('Z', '+00:00'))
            return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
        except ValueError:
            bt.logging.error(f"Invalid date format: {date_string}")
            return datetime.min.replace(tzinfo=timezone.utc)

    def format_event_start_date(self, event_start_date):
        dt = self.parse_date(event_start_date)
        return dt.strftime("%Y-%m-%d %H:%M")

    def update_total_pages(self):
        """
        Update the total number of pages for games pagination.

        Behavior:
            - Calculates the total number of pages based on the number of games and games per page
            - Ensures the current page is within the valid range
        """
        self.total_pages = max(1, (len(self.sorted_games) + self.games_per_page - 1) // self.games_per_page)
        self.current_page = min(self.current_page, self.total_pages - 1)

    def update_text_area(self):
        """
        Update the text area for the games view.

        Behavior:
            - Formats the header, options, and pagination information
            - Updates the text area content
        """
        self.app.reload_miner_stats()  # Reload stats before updating text area
        header_text = self.header
        divider = "-" * len(header_text)
        if len(self.options) == 2:  # Only "Filter" and "Go Back" are present
            options_text = "No games available."
        else:
            options_text = "\n".join(
                f"> {option}" if i == self.selected_index else f"  {option}"
                for i, option in enumerate(self.options[:-2])
            )
        current_time_text = f"\n\nCurrent Time (UTC): {datetime.now(pytz.utc).strftime('%Y-%m-%d %H:%M')}"
        page_info = f"\nPage {self.current_page + 1}/{self.total_pages} (Use left/right arrow keys to navigate)"
        go_back_text = (
            f"\n\n  {self.options[-1]}"
            if self.selected_index != len(self.options) - 1
            else f"\n\n> {self.options[-1]}"
        )
        filter_text = (
            f"\n\n  {self.options[-2]}"
            if self.selected_index != len(self.options) - 2
            else f"\n\n> {self.options[-2]}"
        )
        self.text_area.text = (
            f"{header_text}\n{divider}\n{options_text}{filter_text}{go_back_text}{current_time_text}{page_info}"
        )

    def handle_enter(self):
        """
        Handle the enter key press in the games view.

        Behavior:
            - If the "Go Back" option is selected, changes the view to the main menu
            - If the "Filter" option is selected, cycles through the available sports filters
            - Otherwise, opens the wager confirmation view for the selected game
        """
        if self.selected_index == len(self.options) - 1:  # Go Back
            self.app.change_view(MainMenu(self.app))
        elif self.selected_index == len(self.options) - 2:  # Filter option
            self.cycle_filter()
        else:
            # Get the currently selected game data from the sorted list
            game_index = self.current_page * self.games_per_page + self.selected_index
            if game_index < len(self.sorted_games):
                selected_game_data = self.sorted_games[game_index]
                # Change view to WagerConfirm, passing the selected game data
                self.app.change_view(WagerConfirm(self.app, selected_game_data, self))

    def cycle_filter(self):
        """
        Cycle through the available sports filters.

        Behavior:
            - Updates the current filter to the next available sport
            - If the end of the list is reached, cycles back to "All Sports"
            - Updates the sorted games, total pages, and text area
        """
        current_index = self.available_sports.index(self.current_filter) if self.current_filter != "All Sports" else -1
        next_index = (current_index + 1) % (len(self.available_sports) + 1)
        self.current_filter = self.available_sports[next_index] if next_index < len(self.available_sports) else "All Sports"
        self.update_sorted_games()
        self.update_total_pages()
        self.current_page = 0
        self.selected_index = 0
        self.update_options()
        self.update_text_area()

    def move_up(self):
        """
        Move the selection up in the games view.

        Behavior:
            - Decrements the selected index if not at the top
            - Updates the text area
        """
        if self.selected_index > 0:
            self.selected_index -= 1
            self.update_text_area()

    def move_down(self):
        """
        Move the selection down in the games view.

        Behavior:
            - Increments the selected index if not at the bottom
            - Updates the text area
        """
        if self.selected_index < len(self.options) - 1:
            self.selected_index += 1
            self.update_text_area()

    def move_left(self):
        """
        Move to the previous page in the games view.

        Behavior:
            - Decrements the current page if not on the first page
            - Resets the selected index to 0
            - Updates the options and text area
        """
        if self.current_page > 0:
            self.current_page -= 1
            self.selected_index = 0
            self.update_options()
            self.update_text_area()

    def move_right(self):
        """
        Move to the next page in the games view.

        Behavior:
            - Increments the current page if not on the last page
            - Resets the selected index to 0
            - Updates the options and text area
        """
        if self.current_page < self.total_pages - 1:
            self.current_page += 1
            self.selected_index = 0
            self.update_options()
            self.update_text_area()




class WagerConfirm(InteractiveTable):
    """
    Wager confirmation view
    """

    def __init__(self, app, game_data, previous_view, wager_amount=""):
        """
        Initialize the WagerConfirm view.

        Args:
            app: The main Application instance.
            game_data: Data for the game being wagered on.
            previous_view: The view to return to after confirmation.
            wager_amount: Initial wager amount (default is empty string).

        Behavior:
            - Sets up the wager confirmation view
            - Initializes the wager input field
            - Sets up options for confirming or canceling the wager
        """
        super().__init__(app)
        app.reload_miner_stats()  # Reload stats when initializing WagerConfirm
        self.game_data = game_data
        self.previous_view = previous_view
        self.miner_cash = app.miner_stats["miner_cash"]
        self.selected_team = game_data.teamA  # Default to teamA
        self.wager_input = TextArea(
            text=str(wager_amount),
            multiline=False,
            password=False,
            focusable=True,
        )
        self.options = [
            "Change Selected Team",
            "Enter Wager Amount",
            "Confirm Wager",
            "Go Back",
        ]
        self.confirmation_message = ""
        self.update_text_area()

    def update_text_area(self):
        """
        Update the text area for the wager confirmation view.

        Behavior:
            - Formats the game info, miner cash, selected team, wager amount, and options
            - Updates the text area content
        """
        self.app.reload_miner_stats()  # Reload stats before updating text area
        game_info = (
            f" {self.game_data.sport} | {self.game_data.teamA} vs {self.game_data.teamB} | {self.game_data.eventStartDate} | "
            f"Team A Odds: {self.game_data.teamAodds} | Team B Odds: {self.game_data.teamBodds} | Tie Odds: {self.game_data.tieOdds}"
        )
        cash_info = f"Miner's Cash: ${self.miner_cash}"
        selected_team_text = f"Selected Team: {self.selected_team}"
        wager_input_text = f"Wager Amount: {self.wager_input.text}"
        options_text = "\n".join(
            f"> {option}" if i == self.selected_index else f"  {option}"
            for i, option in enumerate(self.options)
        )
        self.text_area.text = f"{game_info}\n{cash_info}\n{selected_team_text}\n{wager_input_text}\n{options_text}\n\n{self.confirmation_message}"
        self.box = HSplit([self.text_area, self.wager_input])

    def handle_enter(self):
        if self.selected_index == 0:  # Change Selected Team
            self.toggle_selected_team()
        elif self.selected_index == 1:  # Enter Wager Amount
            self.focus_wager_input()
        elif self.selected_index == 2:  # Confirm Wager
            try:
                wager_amount = float(self.wager_input.text.strip())
                if wager_amount <= 0 or wager_amount > self.miner_cash:
                    raise ValueError("Invalid wager amount")
                
                prediction_id = str(uuid.uuid4())
                prediction = {
                    "predictionID": prediction_id,
                    "teamGameID": self.game_data.externalId,  # Use externalId here
                    "minerID": str(self.app.miner_uid),
                    "predictionDate": datetime.now(timezone.utc).isoformat(),
                    "predictedOutcome": self.selected_team,
                    "wager": wager_amount,
                    "teamAodds": float(self.game_data.teamAodds),
                    "teamBodds": float(self.game_data.teamBodds),
                    "tieOdds": float(self.game_data.tieOdds) if self.game_data.tieOdds is not None else None,
                    "outcome": "Unfinished",
                    "teamA": self.game_data.teamA,
                    "teamB": self.game_data.teamB
                }
                self.app.add_prediction(prediction)
                
                try:
                    self.confirmation_message = "Wager submitted successfully!"
                    self.text_area.text = self.confirmation_message
                    self.update_text_area()
                    time.sleep(.5)
                    self.app.change_view(GamesList(self.app))
                except Exception as e:
                    bt.logging.error(f"Error submitting wager: {str(e)}")
                    self.confirmation_message = f"Error submitting wager: {str(e)}"
                    self.update_text_area()
            except ValueError as e:
                self.confirmation_message = str(e)
                self.update_text_area()
        elif self.selected_index == 3:  # Go Back
            self.app.change_view(self.previous_view)

    def move_up(self):
        """
        Move the selection up in the wager confirmation view.

        Behavior:
            - Decrements the selected index if not at the top
            - Updates the text area
        """
        if self.selected_index > 0:
            self.selected_index -= 1
            self.update_text_area()

    def move_down(self):
        """
        Move the selection down in the wager confirmation view.

        Behavior:
            - Increments the selected index if not at the bottom
            - Updates the text area
        """
        if self.selected_index < len(self.options) - 1:
            self.selected_index += 1
            self.update_text_area()

    def focus_wager_input(self):
        """
        Focus the wager input field.

        Behavior:
            - Sets the focus to the wager input field
        """
        self.app.layout.focus(self.wager_input)


    def blur_wager_input(self):
        """
        Blur the wager input field.

        Behavior:
            - Removes the focus from the wager input field
        """
        self.app.layout.focus(self.text_area)

    def handle_wager_input_enter(self):
        """
        Handle the enter key press in the wager input field.

        Behavior:
            - Blurs the wager input field
            - Moves the focus to the "Confirm Wager" option
            - Updates the text area
            - Ensures the focus is back on the text area
        """
        self.blur_wager_input()
        self.selected_index = 2  # Move focus to "Confirm Wager"
        self.update_text_area()
        self.app.layout.focus(self.text_area)  # Ensure focus is back on the text area

    def toggle_selected_team(self):
        """
        Toggle the selected team.

        Behavior:
            - Cycles through the available teams (teamA, teamB, and Tie if applicable)
            - Updates the text area
        """
        if self.selected_team == self.game_data.teamA:
            self.selected_team = self.game_data.teamB
        elif self.selected_team == self.game_data.teamB and self.game_data.canTie:
            self.selected_team = "Tie"
        else:
            self.selected_team = self.game_data.teamA
        self.update_text_area()

if __name__ == "__main__":
    app = None
    try:
        app = Application()
        app.run()
    except Exception as e:
        bt.logging.error(f"Unhandled exception: {e}")
        bt.logging.error(traceback.format_exc())  # Add this line to get the full traceback
