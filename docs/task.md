Build a lightweight, distributed multi-agent mesh architecture using NATS as the event-driven backbone and an orchestrator (like Ormica state-machine pattern) on top. 

Key Requirements:
1. Backbone: NATS server running via Docker handling asynchronous Pub/Sub messaging and Request-Reply patterns between nodes.
2. Orchestration: State-managed workflow engine (Ormica-style) that assigns agent roles (e.g., Coder, Critic, Planer), tracks execution states, and routes payloads.
3. Tooling & Context: Model Context Protocol (MCP) servers on each node providing local tools (shell execution, file access) and structured JSON communication.
4. Inference Backend: Distributed nodes pointing to local Ollama instances (leveraging hardware like AMD ROCm iGPUs/VRAM).
5. Deployment: Minimalist Python-based worker scripts and Docker-Compose setup without heavy agent wrappers or cloud dependencies.
