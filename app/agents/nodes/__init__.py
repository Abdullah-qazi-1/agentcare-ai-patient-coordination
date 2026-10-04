"""Graph nodes.

Each node is a thin adapter: it slices the state into a focused instruction, invokes one
agent, extracts that agent's structured output, and writes the result back into both the
graph state and the database. Keeping the adapters thin is what makes the pipeline
readable as a pipeline — the intelligence lives in the agents, the sequence lives in the
graph, and neither leaks into the other.
"""
