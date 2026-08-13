"""Reading uploaded study material.

Three steps, each in its own module and each testable without a database:

    detect.py   what is this file, really?
    extract.py  the text, and which page it came from
    chunking.py the text cut into searchable pieces

`app/services/document.py` runs them in order and saves the result.
"""
