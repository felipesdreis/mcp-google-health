"""
conftest.py — Proteção do stdout do pytest contra a substituição feita por server.py.

server.py envolve sys.stdout em io.TextIOWrapper na importação para garantir UTF-8
no Windows. Isso fecha o tmpfile interno do pytest, quebrando a captura de saída.
Importamos server aqui, com .buffer bloqueado, antes que o pytest registre o stream
de captura de fato — assim a substituição nunca ocorre.
"""
import sys


class _NoBufferWrapper:
    """Proxy que oculta o atributo .buffer para impedir io.TextIOWrapper wrap."""
    __slots__ = ("_wrapped",)

    def __init__(self, f):
        object.__setattr__(self, "_wrapped", f)

    def __getattr__(self, name):
        if name == "buffer":
            raise AttributeError("buffer")
        return getattr(object.__getattribute__(self, "_wrapped"), name)


_saved_stdout = sys.stdout
_saved_stderr = sys.stderr

sys.stdout = _NoBufferWrapper(sys.stdout)
sys.stderr = _NoBufferWrapper(sys.stderr)

import server  # noqa: E402, F401 — deve ocorrer com .buffer bloqueado

sys.stdout = _saved_stdout
sys.stderr = _saved_stderr
