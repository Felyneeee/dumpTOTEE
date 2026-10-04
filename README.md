# dumpTOTEE
test of tutor-tutee matching

Run it:

    pip install -r requirements.txt
    python app.py

Open http://127.0.0.1:5000

Options (environment variables): PORT, HOST, FLASK_DEBUG=1, SECRET_KEY, TUTOR_DB (database file path).

Tests: `python -m unittest discover tests -v`

Edit subjects / branches / grade levels / locations in `catalog.py`; scoring weights and filters in `matching.py`.
Existing databases are migrated automatically. Old profiles must be re-saved once (new subject, grade and schedule fields).
