# Dallas College Chatbot Architecture Report

## System Topology
This application runs a zero-node-dependency hybrid AI architecture optimized for ultra-low token consumption and secure key-rotation.


## Architectural Invariants
1. **Frontend Isolation:** Client assets are plain HTML5/ES6 Javascript served statically. No client-side builds required.
2. **Deterministic Context (Megaprompt Guard):** The LLM operates with a zero-knowledge parameter. It cannot pull from general training weights and must emit exact fallback strings when data matches fail.
3. **Keyword Chunk Optimization:** Prior to model inference, raw JSON data is sliced based on alphanumeric rubric parsing (`BCIS`, `ITSC`, `COSC`) to maintain minimal context-window costs.