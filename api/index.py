"""
Vercel serverless entry point.
Imports the Flask app from the project root and exposes it as `handler`,
which is what Vercel's Python builder looks for.
"""
import sys
import os

# Make sure imports from the project root (app.py, data.py) work correctly.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app

handler = app
