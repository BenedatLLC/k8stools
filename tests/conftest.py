# pytest configuration to ensure 'k8stools' is importable
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../src')))
