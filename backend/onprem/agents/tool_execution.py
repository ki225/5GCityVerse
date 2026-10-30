"""Bounded LangGraph fan-out/fan-in for an already validated read batch."""
from operator import add
from typing import Annotated, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send
from langsmith import tracing_context


class _BatchState(TypedDict):
    results: Annotated[list, add]


class ParallelReadTools:
    """Drain every read, then return results in the model's original call order.

    Authorization and tool classification belong to RoleAgent. This ephemeral
    graph has no checkpoint or write tools; the enclosing role owns durable
    audit and the parent graph owns recovery.
    """

    def __init__(self, maximum_concurrency=4):
        if type(maximum_concurrency) is not int or not 1 <= maximum_concurrency <= 8:
            raise ValueError('Invalid parallel read concurrency')
        self.maximum_concurrency = maximum_concurrency

    def run(self, calls, execute):
        def read(state):
            index = state['index']
            try:
                result, error = execute(calls[index]), None
            except Exception as exc:
                # Returning the failure lets sibling reads finish before the
                # role fails. No model continuation or terminal can race them.
                result, error = None, exc
            return {'results': [(index, result, error)]}

        builder = StateGraph(_BatchState)
        builder.add_node('read', read)
        builder.add_conditional_edges(START,
            lambda _: [Send('read', {'index': index}) for index in range(len(calls))], ['read'])
        builder.add_edge('read', END)
        graph = builder.compile(checkpointer=False, name='cityverse_precheck_read_batch')
        with tracing_context(enabled=False):
            completed = graph.invoke({'results': []}, {'max_concurrency': self.maximum_concurrency})
        ordered = sorted(completed['results'], key=lambda row: row[0])
        for _, _, error in ordered:
            if error is not None:
                raise error
        return [result for _, result, _ in ordered]
