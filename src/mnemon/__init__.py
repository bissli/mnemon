from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version('mnemon')
except PackageNotFoundError:
    __version__ = 'dev'
