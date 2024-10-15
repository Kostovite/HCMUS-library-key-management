import re
import sqlite3
from datetime import datetime
import tkinter as tk
from tkinter import ttk, messagebox
import threading

FIRST_KEY_ID = 1
LAST_KEY_ID = 100

DATABASE_NAME = 'library_key_management.db'
TIME_FORMAT = "%Y-%m-%d %H:%M:%S"

# Adapter: Convert datetime to string when storing in the database
def adapt_datetime(dt):
    return dt.strftime(TIME_FORMAT)

# Converter: Convert string back to datetime when retrieving from the database
def convert_datetime(s):
    return datetime.strptime(s.decode(), TIME_FORMAT)

sqlite3.register_adapter(datetime, adapt_datetime)
sqlite3.register_converter("timestamp", convert_datetime)

class LibraryKeyManagement:
    def __init__(self, db_name=DATABASE_NAME):
        self.current_student = None
        self.conn = sqlite3.connect(db_name, detect_types=sqlite3.PARSE_DECLTYPES, check_same_thread=False)
        self.cursor = self.conn.cursor()

        # We no longer initialize keys in memory; they will be loaded from the database
        self.available_keys = set()
        self.borrowed_keys = set()

        self._create_tables()
        self._load_keys_from_db()

    def _create_tables(self):
        # Create the tables if they don't exist
        self.cursor.execute('''
        CREATE TABLE IF NOT EXISTS student_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id TEXT NOT NULL,
            entry_time TIMESTAMP NOT NULL,
            key_id INTEGER DEFAULT NULL,
            key_status TEXT DEFAULT NULL
        )
        ''')

        self.cursor.execute('''
        CREATE TABLE IF NOT EXISTS key_status (
            key_id INTEGER PRIMARY KEY,
            status TEXT NOT NULL CHECK (status IN ('Available', 'Borrowed'))
        )
        ''')

        # Initialize the key_status table with keys in the range if empty
        self.cursor.execute('SELECT COUNT(*) FROM key_status')
        count = self.cursor.fetchone()[0]
        if count == 0:
            self.cursor.executemany('''
            INSERT INTO key_status (key_id, status)
            VALUES (?, 'Available')
            ''', [(key_id,) for key_id in range(FIRST_KEY_ID, LAST_KEY_ID + 1)])

        self.conn.commit()

    def _load_keys_from_db(self):
        """Load available and borrowed keys from the key_status table into RAM."""
        self.cursor.execute('SELECT key_id, status FROM key_status')
        keys = self.cursor.fetchall()

        for key_id, status in keys:
            if status == 'Borrowed':
                self.borrowed_keys.add(key_id)
            elif status == 'Available':
                self.available_keys.add(key_id)

    def _update_key_status_in_db(self, key_id, status):
        """Perform the key update in the database asynchronously."""
        def update():
            with sqlite3.connect(DATABASE_NAME, detect_types=sqlite3.PARSE_DECLTYPES, check_same_thread=False) as conn:
                cursor = conn.cursor()
                cursor.execute('''
                INSERT INTO key_status (key_id, status) 
                VALUES (?, ?)
                ON CONFLICT(key_id) DO UPDATE SET status = excluded.status
                ''', (key_id, status))
                conn.commit()

        # Run the database update in a separate thread
        threading.Thread(target=update, daemon=True).start()
    
    def _process_student_id(self, student_id):
        self.current_student = student_id
        self.cursor.execute('''
        INSERT INTO student_entries (student_id, entry_time, key_id, key_status)
        VALUES (?, ?, NULL, NULL)
        ''', (student_id, datetime.now()))
        self.conn.commit()
        return f"Student {student_id} entered the library."

    def _process_key_id(self, key_id):
        """Process the scanned key."""
        if not self.current_student:
            return "Error: No student ID scanned. Please scan a student ID first."

        key_id = int(key_id)

        if key_id in self.borrowed_keys:
            # Return the key
            self.cursor.execute('''
            UPDATE student_entries
            SET key_id = ?, key_status = 'Returned'
            WHERE id = (SELECT id FROM student_entries WHERE key_id = ? AND key_status = 'Borrowed' ORDER BY entry_time DESC LIMIT 1)
            ''', (key_id, key_id))
            self.conn.commit()

            # Update in memory
            self.borrowed_keys.remove(key_id)
            self.available_keys.add(key_id)

            # Async update in the database
            self._update_key_status_in_db(key_id, 'Available')

            return f"Key {key_id} returned."
        
        if key_id in self.available_keys:
            # Check if the student already has a borrowed key
            self.cursor.execute('''
            SELECT key_id FROM student_entries
            WHERE student_id = ? AND key_status = 'Borrowed'
            ''', (self.current_student,))
            active_borrowed_key = self.cursor.fetchone()

            if active_borrowed_key and active_borrowed_key[0] != key_id:
                return f"Error: Student {self.current_student} already has key {active_borrowed_key[0]} borrowed. Return it before borrowing another key."

            # Borrow the key
            self.cursor.execute('''
            UPDATE student_entries
            SET key_id = ?, key_status = 'Borrowed'
            WHERE id = (SELECT id FROM student_entries WHERE student_id = ? AND (key_status IS NULL OR key_status = 'Returned') ORDER BY entry_time DESC LIMIT 1)
            ''', (key_id, self.current_student))
            self.conn.commit()

            # Update in memory
            self.available_keys.remove(key_id)
            self.borrowed_keys.add(key_id)

            # Async update in the database
            self._update_key_status_in_db(key_id, 'Borrowed')

            return f"Key {key_id} borrowed by student {self.current_student}."

    def get_status(self):
        # Get keys with their status from the database
        self.cursor.execute('''
        SELECT student_id, key_id, key_status
        FROM student_entries
        WHERE key_status = 'Borrowed'
        ''')
        borrowed_keys = self.cursor.fetchall()

        # Create a dictionary to keep track of borrowed keys with student IDs
        borrowed_dict = {key[1]: key[0] for key in borrowed_keys}  # key_id -> student_id

        # Create a list of all keys with their status (whether borrowed or available)
        status_list = []
        for key_id in range(FIRST_KEY_ID, LAST_KEY_ID + 1):
            if key_id in borrowed_dict:
                status_list.append((key_id, 'Borrowed', borrowed_dict[key_id]))  # Key is borrowed
            else:
                status_list.append((key_id, 'Available', None))  # Key is available

        return status_list

    def get_log(self, limit=50):
        self.cursor.execute('''
        SELECT student_id, key_id, entry_time, key_status
        FROM student_entries
        ORDER BY entry_time DESC
        LIMIT ?
        ''', (limit,))
        return self.cursor.fetchall()

    def __del__(self):
        self.conn.close()

class LibraryKeyManagementGUI:
    def __init__(self, master):
        self.master = master
        self.master.title("Library Key Management System")
        self.system = LibraryKeyManagement()

        self.current_student_id = None  # Track the current student ID
        self.create_widgets()

        # Refresh the data on initial load
        self.refresh_data()

    def create_widgets(self):
        # Input frame
        input_frame = ttk.Frame(self.master, padding="10")
        input_frame.grid(row=0, column=0, sticky=(tk.W, tk.E, tk.N, tk.S))

        ttk.Label(input_frame, text="Scan ID:").grid(row=0, column=0, sticky=tk.W)
        self.input_entry = ttk.Entry(input_frame, width=30)
        self.input_entry.grid(row=0, column=1, sticky=(tk.W, tk.E))
        self.input_entry.focus()

        ttk.Button(input_frame, text="Process", command=self.process_input).grid(row=0, column=2, padx=5)

        # Message display
        self.message_var = tk.StringVar()
        ttk.Label(input_frame, textvariable=self.message_var, wraplength=300).grid(row=1, column=0, columnspan=3, pady=10)

        # Notebook for Log and Status
        notebook = ttk.Notebook(self.master)
        notebook.grid(row=1, column=0, sticky=(tk.W, tk.E, tk.N, tk.S))

        # Log tab
        log_frame = ttk.Frame(notebook, padding="10")
        self.log_tree = self.create_treeview(log_frame, ['Student ID', 'Key ID', 'Time', 'Key Status'])
        notebook.add(log_frame, text='Log')

        # Status tab
        status_frame = ttk.Frame(notebook, padding="10")

        # Add Combobox for filtering
        self.filter_var = tk.StringVar(value="All")
        filter_combobox = ttk.Combobox(status_frame, textvariable=self.filter_var, values=["All", "Borrowed", "Available"], state="readonly")
        filter_combobox.pack(padx=5, pady=5, fill=tk.X)  # Use pack here

        filter_combobox.bind("<<ComboboxSelected>>", lambda event: self.refresh_data())

        self.status_tree = self.create_treeview(status_frame, ['Key ID', 'Status', 'Student ID'])
        notebook.add(status_frame, text='Status')

        # Set default tab to Log
        notebook.select(log_frame)

        # Refresh button
        ttk.Button(self.master, text="Refresh", command=self.refresh_data).grid(row=2, column=0, pady=10)

        # Configure grid
        self.master.columnconfigure(0, weight=1)
        self.master.rowconfigure(1, weight=1)

        # Bind return key to process_input
        self.master.bind('<Return>', lambda event: self.process_input())

    def create_treeview(self, parent, columns):
        tree = ttk.Treeview(parent, columns=columns, show='headings')
        for col in columns:
            tree.heading(col, text=col)
            tree.column(col, width=100)
        
        # Set the tree to expand and fill the space in both directions
        tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        scrollbar = ttk.Scrollbar(parent, orient=tk.VERTICAL, command=tree.yview)
        tree.configure(yscroll=scrollbar.set)
        
        # Pack the scrollbar to the right
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        return tree

    def process_input(self):
        input_id = self.input_entry.get().strip()
        if input_id:
            if self.is_valid_student_id(input_id):
                result = self.system._process_student_id(input_id)
                self.current_student_id = input_id  # Set the current student ID
            elif self.is_valid_key_id(input_id):
                if self.current_student_id:
                    result = self.system._process_key_id(input_id)
                else:
                    result = "Error: No student ID scanned. Please scan a student ID first."
            else:
                result = "Error: Invalid ID format. Please enter a valid student ID or key ID."

            self.message_var.set(result)
            self.input_entry.delete(0, tk.END)
            self.refresh_data()

    def is_valid_student_id(self, student_id):
        return bool(re.match(r'^\d{8}$', student_id))

    def is_valid_key_id(self, key_id):
        return key_id.isdigit() and FIRST_KEY_ID <= int(key_id) <= LAST_KEY_ID

    def refresh_data(self):
        # Clear current entries in the status tree
        for i in self.status_tree.get_children():
            self.status_tree.delete(i)

        # Get filter selection
        filter_value = self.filter_var.get()

        # Get status data based on filter
        if filter_value == "All":
            status_data = self.system.get_status()  # Retrieve all entries
        elif filter_value == "Borrowed":
            status_data = [row for row in self.system.get_status() if row[1] == 'Borrowed']
        elif filter_value == "Available":
            status_data = [row for row in self.system.get_status() if row[1] == 'Available']
        
        for row in status_data:
            key_id, status, student_id = row
            self.status_tree.insert('', 'end', values=(key_id, status, student_id or "N/A"))

        # Clear current entries in the log tree
        for i in self.log_tree.get_children():
            self.log_tree.delete(i)
        for row in self.system.get_log():
            self.log_tree.insert('', 'end', values=row)

if __name__ == "__main__":
    root = tk.Tk()
    app = LibraryKeyManagementGUI(root)
    root.mainloop()