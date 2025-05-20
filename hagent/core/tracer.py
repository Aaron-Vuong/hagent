import datetime
import functools
import inspect
import json
import litellm
import threading
import time

from abc import ABCMeta
from enum import Enum
from typing import (
    List,
    Tuple,
)

from litellm.integrations.custom_logger import CustomLogger
from ruamel.yaml import YAML

# Keep everything under specific tabs in the Perfetto timeline.
HAGENT_PID = 0
HAGENT_TID = 0
LLM_PID = 1
LLM_TID = 1
METADATA_TID = 2

def s_to_us(s: float) -> float:
    """
    Convert seconds to microseconds.
    """
    return s * 1_000_000

# https://docs.python.org/3/howto/logging-cookbook.html#implementing-structured-logging
class Encoder(json.JSONEncoder):
    def default(self, o):
        if isinstance(o, set):
            return tuple(o)
        elif isinstance(o, str):
            return o.encode('unicode_escape').decode('ascii')
        return super().default(o)

def read_yaml(input_file: str) -> dict:
    """
    Reads an input YAML file and attempts to convert it to a Python dictionary.

    Args:
        input_file: The input YAML file to convert to a dictionary.

    Returns:
        A dictionary representation of the YAML file.
        If loading fails, a dictionary with 'error' as its only key is returned instead.

    """
    try:
        yaml_obj = YAML(typ='safe')
        with open(input_file, 'r') as f:
            data = yaml_obj.load(f)
    except Exception:
        return {'error': ''}
    return data

#####################
## TRACER PERFETTO ##
#####################
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
        
        if isinstance(ph, str):
            ph = PhaseType(ph)

        self.name = name
        self.cat = cat
        self.ph = ph
        self.ts = ts
        self.pid = pid
        self.tid = tid
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
        # Delete any optional values so they don't pollute the trace.
        pruned_d = {}
        for key, val in self.__dict__.items():
            if val is not None:
                pruned_d[key] = val
        pruned_d["ph"] = pruned_d["ph"].value
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
    enabled = True

    @classmethod
    def disable(cls):
        """
        Disables any tracing for this execution.
        """
        cls.enabled = False
        cls.events.clear()
    
    @classmethod
    def enable(cls):
        """
        Enables any tracing for this execution.
        """
        cls.enabled = True

    @classmethod
    def get_events(cls) -> List[TraceEvent]:
        """
        Gets the event trace.
        """
        return [event.to_json() for event in cls.events]

    @classmethod
    def log(cls, event: TraceEvent):
        """
        Add a new event.
        """
        if cls.enabled:
            cls.events.append(event)

    @classmethod
    def add_flow_events(cls, dependencies: Tuple[set, set, set]):
        """
        Adds the Flow TraceEvents to provide relations between events.

        Args:
            dependencies: Three sets of YAML files
            - initial YAML files (no dependencies)
            - input YAML files (input(s) to a Step),
            - output files (output of a Step).

        """
        initial, inputs, outputs = dependencies
        print(initial)
        pipe_id = 0
        # Each non-initial YAML file is a record of a Step execution.
        for event in cls.events:
            if event.cat != "hagent.step":
                continue
            data = event.args["data"]
            if data.keys == ["error"]:
                print("Failure detected in step!")
                continue

            # TODO: figure out support for multi-pipe flows.
            flow_name = f"pipe_{pipe_id}"

            # If the input of this Step was an initial configuration file,
            # this step has no dependencies.

            # This will automatically create an array of input files if one was not given.
            if isinstance(data['tracing']['input'], str):
                data['tracing']['input'] = [data['tracing']['input']]

            print(f"INPUTS: {set(data['tracing']['input'])}: {set(data['tracing']['input']).intersection(initial)}")
            if set(data['tracing']['input']).intersection(initial):
                Tracer.log(TraceEvent(
                    name = flow_name,
                    cat = "hagent",
                    ph = PhaseType.FLOW_START,
                    ts = event.ts,
                    pid = HAGENT_PID,
                    tid = event.tid,
                    id = pipe_id)
                )
            # If the output of this Step was an input of another Step,
            # then we know this is an intermediate Step.
            elif data['tracing']["output"] in inputs:
                Tracer.log(TraceEvent(
                    name = flow_name,
                    cat = "hagent",
                    ph = PhaseType.FLOW_STEP,
                    ts = event.ts,
                    pid = HAGENT_PID,
                    tid = event.tid,
                    id = pipe_id)
                )
            else:
                Tracer.log(TraceEvent(
                    name = flow_name,
                    cat = "hagent",
                    ph = PhaseType.FLOW_END,
                    ts = event.ts,
                    pid = HAGENT_PID,
                    tid = event.tid,
                    id = pipe_id,
                    bp="e")
                )

    @classmethod
    def add_metadata(cls, asynchronous: bool):
        """
        Adds metadata events to rename and prettify the Perfetto Trace.
        """
        # Add thread names.
        thread_metadata = []
        for event in cls.events:
            if event.cat != "hagent.step":
                continue
            # TODO: Handle multi-thread.

        if not thread_metadata:
            Tracer.log(TraceEvent(
                name = "thread_name",
                cat = "__metadata",
                ph = PhaseType.METADATA,
                ts = 0,
                pid = HAGENT_PID,
                tid = HAGENT_TID,
                args = {
                    "name": "Pipe"
                },
            ))
            Tracer.log(TraceEvent(
                name = "thread_name",
                cat = "__metadata",
                ph = PhaseType.METADATA,
                ts = 0,
                pid = LLM_PID,
                tid = LLM_TID,
                args = {
                    "name": "LLM_Completions"
                },
            ))

        # Name overarching categories (Hagent + LLM).
        Tracer.log(TraceEvent(
            name = "process_name",
            cat = "__metadata",
            ph = PhaseType.METADATA,
            ts = 0,
            pid = HAGENT_PID,
            tid = METADATA_TID,
            args = {
                "name":"Hagent"
            },
        ))
        Tracer.log(TraceEvent(
            name = "process_name",
            cat = "__metadata",
            ph = PhaseType.METADATA,
            ts = 0,
            pid = LLM_PID,
            tid = METADATA_TID,
            args = {
                "name": "LiteLLM"
            },
        ))
    
    @classmethod
    def create_asynchronous_trace(cls, dependencies: Tuple[set, set, set]):
        """
        Creates an asynchronous trace from the recorded events.

        Args:
            dependencies: Three sets of YAML files
            - initial YAML files (no dependencies)
            - input YAML files (input(s) to a Step),
            - output files (output of a Step).

        """
        # Put every Step into a separate thread.
        steps = []
        # Collect all Steps.
        for event in cls.events:
            if event.cat == "hagent.step":
                steps.append(event)

        # Align timestamps to the same level in the dependency tree.
        for event in cls.events:
            event.tid = event["args"]["step_id"]

        raise NotImplementedError

    @classmethod
    def save_perfetto_trace(cls, dependencies: Tuple[set, set, set], filename: str=None, asynchronous: bool=False):
        """
        Saves the events off in a Perfetto-compatible JSON file.

        Args:
            asynchronous: Dump a trace where all events are not displayed as recorded.
                         The trace will do a best-effort re-ordering to depict
                         a fully asynchronous run.

        """
        if filename is None:
            filename = "hagent.json"
        # TODO: Modify the TraceEvents to be fully parallelized.
        if asynchronous:
            cls.create_asynchronous_trace()
        # Add necessary metadata to visualize each event nicely.
        cls.add_metadata(asynchronous)
        # Add Flow TraceEvents to depict how each step flows into the next.
        cls.add_flow_events(dependencies)

        with open(filename, "w+", encoding="utf-8") as f:
            json.dump({
                "traceEvents": [event.to_json() for event in cls.events]
            }, f, indent=2, default=str)

###############
## METACLASS ##
###############
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
        
        # Ensure all arguments (*args, **kwargs) are JSON serializable.
        serialized_args = []
        serialized_kwargs = {}
        for arg in args:
            serialized_args.append(str(arg))
        for key, val in kwargs.items():
            serialized_kwargs[str(key)] = str(val)

        Tracer.log(TraceEvent(
            # This is C++ syntax, but it is a nice, clean way to show a class::method relationship.
            name = f"{args[0].__class__.__name__}::{func.__name__}",
            cat = "hagent",
            ph = PhaseType.COMPLETE,
            ts = s_to_us(start_time),
            pid = HAGENT_PID,
            tid = threading.get_ident(),
            args = {
                "func": func.__name__,
                "func_args": serialized_args,
                "func_kwargs": serialized_kwargs,
                "func_result": str(result)
            },
            dur = s_to_us(end_time - start_time),
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

        You could also do this by generating a cProfile and use another visualizer.

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

class TracerABCMetaClass(ABCMeta, TracerMetaClass):
    """
    Use this MetaClass when using @abstractmethod/for abstract classes.
    """

#############
## LITELLM ##
#############
class TracerHandler(CustomLogger):
    """
    Custom callback for litellm support.
    """
    def log_pre_api_call(self, model, messages, kwargs):
        return
        print(f" <--- Pre-API Call")
        # print(f"MODEL: {model}")
        # print(f"MESSAGES: {messages}")
        # print(f"KWARGS: {kwargs}")
        print(f"PRE API START: {time.time()}")
        
        Tracer.log(TraceEvent(
            name = kwargs["litellm_call_id"] + "_PRE",
            cat = "hagent",
            ph = PhaseType.COMPLETE,
            ts = s_to_us(time.time()),
            pid = 10,
            # TODO: Investigate using something else?
            tid = 5333,
            args = {
                "kwargs": kwargs,
                "messages": messages,
            },
            dur = s_to_us(0),
        ))
        print(f" ---> Pre-API Call")
    
    def log_post_api_call(self, kwargs, response_obj, start_time, end_time):
        return
        print(f" <--- Post-API Call")
        print(f"KWARGS: {kwargs}")
        print(f"RESPONSE_OBJ: {response_obj}")
        print(f"START: {start_time}")
        print(f"END: {end_time}")
        print(f" ---> Post-API Call")
    

    def log_success_event(self, kwargs, response_obj, start_time, end_time):
        return
        print(f" <--- On Success")
        #print(f"KWARGS: {kwargs}")
        duration = (end_time - start_time).total_seconds()
        #print(f"duration: {duration}")
        print(f"SUCCESS START: {time.time()}")
        print("logging")
        Tracer.log(TraceEvent(
            name = kwargs["litellm_call_id"] + "_SUCCESS",
            cat = "hagent",
            ph = PhaseType.COMPLETE,
            ts = s_to_us(start_time.timestamp()),
            pid = 10,
            # TODO: Investigate using something else?
            tid = 5333,
            args = {
                "kwargs": kwargs,
                "response_obj": response_obj
            },
            dur = s_to_us(duration),
        ))
        print(f" ---> On Success")
        Tracer.save_perfetto_trace()

    def log_failure_event(self, kwargs, response_obj, start_time, end_time):
        return
        print(f" <--- On Failure")
        print(f"KWARGS: {kwargs}")
        print(f"RESPONSE_OBJ: {response_obj}")
        print(f"START: {start_time}")
        print(f"END: {end_time}")
        print(f" ---> On Failure")
    
    #### ASYNC #### - for acompletion/aembeddings

    async def async_log_success_event(self, kwargs, response_obj, start_time, end_time):
        return
        print(f" <--- On Async Success")
        print(f"KWARGS: {kwargs}")
        print(f"RESPONSE_OBJ: {response_obj}")
        print(f"START: {start_time}")
        print(f"END: {end_time}")
        print(f" ---> On Async Success")

    async def async_log_failure_event(self, kwargs, response_obj, start_time, end_time):
        return
        print(f" <--- On Async Failure")
        print(f"KWARGS: {kwargs}")
        print(f"RESPONSE_OBJ: {response_obj}")
        print(f"START: {start_time}")
        print(f"END: {end_time}")
        print(f" ---> On Async Failure")

# Add tracing to liteLLM.
#tracer_handler = TracerHandler()
#litellm.callbacks.append(tracer_handler)

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

def trace_inner(func):
    @functools.wraps(func)
    def inner(*args, **kwargs):
        print(inspect.getmembers(inner))
        #inner_functions = [member[1] for member in inspect.getmembers(func, inspect.isfunction) if member[1].__qualname__.startswith(func.__name__+'.')]
        #print(inner_functions)
        result = func(*args, **kwargs)
        return result
    return inner

@trace_inner
def main():
    q = SubSubClass()
    q.f(b=5)
    print(Tracer.events)

if __name__ == "__main__":
    main()
