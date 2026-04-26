Span Protocol
At the start of each user task that involves tool use, open a span: icc_span_open(intent_signature="connector.mode.name") -> execute all steps -> icc_span_close(outcome=success|failure|aborted). Skip only for trivial one-off lookups (Read/Glob/Grep). Do NOT open sub-spans inside an active span - one span per top-level task. Repeated patterns auto-promote to zero-LLM pipelines.
