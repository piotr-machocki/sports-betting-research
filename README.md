# Setup

This guide explains how to set up the project from a fresh clone. The Betclic integration is not included in the public repository.

## Requirements

* Python 3.12 or newer (`python --version`)
* Git
* A PinnAPI key (available at [PinnAPI](https://pinnapi.com))

## 1. Clone the repository

```bash
git clone https://github.com/piotr-machocki/sports-betting-research.git
cd sports-betting-research
```

## 2. Create and activate a virtual environment

```bash
python -m venv .venv
```

Activate it:

* Windows (PowerShell): `.venv\Scripts\Activate.ps1`
* Windows (Command Prompt): `.venv\Scripts\activate.bat`
* macOS / Linux: `source .venv/bin/activate`

Your terminal prompt should now start with `(.venv)`.

## 3. Install dependencies

```bash
pip install -e .
playwright install chromium
```

## 4. Get a PinnAPI key

1. Go to [PinnAPI](https://pinnapi.com) and create an account.
2. Open your account dashboard and generate an API key.
3. Copy the key.

## 5. Configure environment variables

Create a `.env` file in the repository root with the following content:

```dotenv
PINNAPI_KEY=your_key_here
```

Replace `your_key_here` with your actual API key. Do not commit your `.env` file.

## 6. Add the Betclic integration

The Betclic integration is not included in the public repository. If you have been provided with `betclic.py`, place it at:

```text
src/betting/books/betclic.py
```

## 7. Run the pipeline

```bash
python scripts/run_once.py
```

The pipeline runs three steps in sequence:

1. Betclic odds collection
2. Pinnacle odds collection
3. Odds comparison

When collecting Betclic odds, a Chromium browser window may open. Keep it open until the collection finishes.


