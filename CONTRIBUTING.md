# Contributing

```bash
python -m venv .venv && . .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt -r requirements-dev.txt
python download_models.py
ruff check . && python -m pytest
python run.py
```

* Anything that decides who can see what (accounts, folders, shares) needs a test in `tests/test_search_acl.py`.
* The front end is plain JavaScript with no build step, and no inline scripts (the CSP forbids them).
* Please use only images you have the right to share in screenshots and tests (the suite uses public-domain images from scikit-image).
