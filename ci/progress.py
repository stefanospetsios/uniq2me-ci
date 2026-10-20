"""Consistent stdout diagnostics identified by workflow stage and function."""
import os
import inspect
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps

_stage = ContextVar('uniq2me_pipeline_stage', default=None)


def current_stage():
    return _stage.get() or os.environ.get('STAGE') or 'controller'


@contextmanager
def stage_context(stage):
    token = _stage.set(stage)
    try:
        yield
    finally:
        _stage.reset(token)


def log(function, message):
    print(f'{current_stage()}.{function}: {message}', flush=True)


def traced(function=None, *, name=None, stage_arg=None, stage_prefix=''):
    """Trace boundaries without changing return values or swallowing failures."""
    def decorate(fn):
        label = name or fn.__name__
        stage_parameter = list(inspect.signature(fn).parameters)[stage_arg] if stage_arg is not None else None
        @wraps(fn)
        def invoke(*args, **kwargs):
            stage = current_stage()
            if stage_arg is not None:
                selected = args[stage_arg] if len(args) > stage_arg else kwargs.get(stage_parameter, stage)
                stage = stage_prefix + str(selected)
            with stage_context(stage):
                log(label, 'START')
                try:
                    result = fn(*args, **kwargs)
                except Exception as error:
                    if not hasattr(error, '_pipeline_function'):
                        error._pipeline_function = label
                    log(label, 'FAILED: ' + str(error))
                    raise
                log(label, 'COMPLETE')
                return result
        return invoke
    return decorate(function) if function is not None else decorate
