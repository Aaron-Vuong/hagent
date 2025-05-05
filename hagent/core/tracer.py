import functools
import json
import threading
import time

from enum import Enum

# Keep everything under one tab in the Perfetto timeline.
HAGENT_ID = 10

# https://docs.python.org/3/howto/logging-cookbook.html#implementing-structured-logging
class Encoder(json.JSONEncoder):
    def default(self, o):
        if isinstance(o, set):
            return tuple(o)
        elif isinstance(o, str):
            return o.encode('unicode_escape').decode('ascii')
        return super().default(o)

class PhaseType(str, Enum):
    """
    Enum for Perfetto PhaseType constants.
    """
    DURATION_BEGIN="B"
    DURATION_END="E"
    COMPLETE="X"
    INSTANT="i"
    COUNTER="C"
    ASYNC_BEGIN="b"
    ASYNC_INSTANT="n"
    ASYNC_END="e"
    FLOW_START="s"
    FLOW_STEP="t"
    FLOW_END="f"
    SAMPLE="P"
    OBJECT_CREATE="N"
    OBEJCT_SNAPSHOT="O"
    OBJECT_DESTROY="D"
    METADATA="M"
    MEM_DUMP_GLOBAL="V"
    MEM_DUMP_PROCESS="v"
    MARK="r"
    CLOCK_SYNC="c"
    CONTEXT="(,)"

class TraceEvent:
    optional_args = ["args", "id", "bp", "dur"]
    def __init__(
            self,
            name: str,
            cat: str,
            ph: PhaseType,
            ts: int,
            pid: int,
            tid: int,
            **kwargs):
        self.__dict__["args"] = {}
        for arg_name in kwargs:
            if arg_name in self.optional_args:
               self.__dict__[arg_name] = kwargs[arg_name]
        
    def to_json(self) -> dict:
        """
        Transform this event into JSON format.

        Returns:
            A dictionary repr of the TraceEvent with non-null values.
        """
        d = self.__dict__

        # Delete any optional values so they don't pollute the trace.
        pruned_d = {}
        for key, val in d.items():
            if val is not None:
                pruned_d[key] = val
        return pruned_d

    def __str__(self) -> str:
        s = Encoder().encode(self.to_json())
        return s
    
    def __repr__(self) -> str:
        return self.__str__()

class Tracer:
    """
    Singleton event handler for TraceEvents.
    """
    events = []
    
    @classmethod
    def log(cls, event: TraceEvent):
        """
        Add a new event.
        """
        cls.events.append(event)

    @classmethod
    def add_flow_events(cls):
        """
        Adds the Flow TraceEvents to provide relations between events.
        """
        raise NotImplementedError
    
    @classmethod
    def add_metadata():
        """
        Adds metadata events to rename and prettify the Perfetto Trace.
        """
        raise NotImplementedError
    
    @classmethod
    def create_asynchronous_trace():
        """
        Creates an asynchronous trace from the recorded events.
        """
        raise NotImplementedError

    @classmethod
    def save_perfetto_trace(cls, filename: str=None, synchronous: bool=True):
        """
        Saves the events off in a Perfetto-compatible JSON file.

        Args:
            synchronous: Dump a trace where all events are displayed as recorded.
                         If not, the trace will do a best-effort re-ordering to depict
                         a fully asynchronous run.

        """
        if filename is None:
            filename = "hagent.json"
        # TODO: Modify the TraceEvents to be fully parallelized.
        if not synchronous:
            cls.create_asynchronous_trace()
        # TODO: Add necessary metadata to visualize each event nicely.
        cls.add_metadata()
        # TODO: Add Flow TraceEvents to depict how each step flows into the next.
        cls.add_flow_events()

        with open(filename, "w+", encoding="utf-8") as f:
            json.dumps({
                "traceEvents": cls.events
            }, f)

def trace_function(func):
    """
    Decorator to provide the Tracer logger with all metadata to construct a trace.
    """
    @functools.wraps(func)
    def inner(*args, **kwargs):
        start_time = time.time()
        result = func(*args, **kwargs)
        end_time = time.time()
        # Mark each function as a complete step.
        # We can augment the log with Flow comments later on.
        # TODO: Check with prof if we want this to use the standard 'logging' module.
        
        # Ensure all arguments (*args, **kwargs) are JSON serializable.
        serialized_args = []
        serialized_kwargs = {}
        for arg in args:
            serialized_args.append(str(arg))
        for key, val in kwargs.items():
            serialized_kwargs[str(key)] = str(val)

        Tracer.log(TraceEvent(
            name = func.__qualname__,
            cat = "hagent",
            ph = PhaseType.COMPLETE,
            ts = time.time(),
            pid = HAGENT_ID,
            # TODO: Investigate using something else?
            tid = threading.get_ident(),
            args = {
                "func": func.__name__,
                "func_args": serialized_args,
                "func_kwargs": serialized_kwargs,
                "func_result": result
            },
            dur = (end_time - start_time),
        ))
        return result
    return inner

# https://stackoverflow.com/a/6307917
class TracerMetaClass(type):
    def __new__(cls, name, bases, local):
        """ A MetaClass to append tracing functionality to a class and its subclasses.
        
        Tracing is done via a decorator that will log the following information.
        - Total time for a function taken.
        - Input arguments.
        - Output arguments.
        This allows us to track what functions are dependent on while being minimally invasive to the codebase.
        This also allows us to track overall function time for each function.

        Ensure that all decorators do not obscure decorated function names (i.e. use functools.wraps(func)). This
        allows the appropriate function name to be displayed in the trace.

        You could also do this by adding cProfile or any other visualizer.

        Example usage that will auto-magically add tracing decorators:
            class BaseClass(metaclass=TracerMetaClass):
                def method_1(self):
                    return 1
                def method_2(self):
                    return 2
        
        Args:
            cls: The class that will be an instance of this TracerMetaClass.
            name: The name of the class.
            bases: The base class of the class.
            local: The attributes of the class as a dictionary.
        
        Returns:
            The constructed class instance.

        """
        for attr in local:
            value = local[attr]

            # Attach the wrapped function in place of any method
            # of the class. This can be further wrapped by any other decorator, but
            # this trace decorator will always be called FIRST out of every decorator.
            # 1. enter this trace_decorator
            # 2.    enter subsequent decorator
            # 3.        run function
            # 4.    exit subsequent decorator
            # 5. exit this trace_decorator
            if callable(value):
                local[attr] = trace_function(value)
        return type.__new__(cls, name, bases, local)

#############
## TESTING ##
#############
def test(func):
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        print("start_test")
        result = func(*args, **kwargs)
        print(result)
        print("test")
        return result
    return wrapper

class Base(metaclass=TracerMetaClass):
    def baz(self):
        print("base")

class SubClass(Base):
    def new_function(self):
        print("_subclass")

class SubSubClass(SubClass):
    @test
    def f(self, b: int):
        print("__subclass")
        return b

q = SubSubClass()
q.f(b=5)
print(Tracer.events)