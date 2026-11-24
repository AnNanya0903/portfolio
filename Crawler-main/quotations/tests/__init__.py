import sys

if sys.version_info >= (3, 14):
    # Django 4.2's test client cannot copy template contexts on Python 3.14 (fixed in newer Django).
    # Production runs Python 3.9 (runtime.txt); this only lets the page tests run on a 3.14 machine.
    from django.template.context import BaseContext

    def _copy_context(self):
        duplicate = self.__class__.__new__(self.__class__)
        duplicate.__dict__.update(self.__dict__)
        duplicate.dicts = self.dicts[:]
        return duplicate

    BaseContext.__copy__ = _copy_context
