import sys, os
_dir = os.path.dirname(os.path.abspath(__file__))
if _dir not in sys.path:
    sys.path.insert(0, _dir)
os.chdir(_dir)
exec(open(os.path.join(_dir, 'main.py'), encoding='utf-8').read())
