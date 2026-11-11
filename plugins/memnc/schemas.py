"""Tool schemas — what the LLM sees."""

MESH_SUBMIT = {
    "name": "mesh_submit",
    "description": (
        "Submit a task to the memnc mesh and wait for the result. The mesh "
        "orchestrator assigns the task to a worker node (planner -> coder -> "
        "critic pipeline); nodes that are down are skipped automatically. "
        "Use for work you want to delegate to the local multi-node mesh."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task": {
                "type": "string",
                "description": "The task goal, as a clear self-contained instruction.",
            },
            "watch": {
                "type": "boolean",
                "description": "Wait for completion and return the result (default true).",
            },
            "node": {
                "type": "string",
                "description": "Optional: pin the task to one node by name.",
            },
            "needs_model": {
                "type": "string",
                "description": "Optional: required model capability tag (e.g. 'vision').",
            },
            "needs_role": {
                "type": "string",
                "description": "Optional: required role tag (e.g. 'coder', 'planner').",
            },
        },
        "required": ["task"],
    },
}

MESH_STATUS = {
    "name": "mesh_status",
    "description": (
        "Show the memnc mesh roster (which nodes are up, their roles and "
        "models) and currently running tasks. Nodes that lost their "
        "heartbeats are not in the rotation."
    ),
    "parameters": {"type": "object", "properties": {}},
}

MESH_STATE = {
    "name": "mesh_state",
    "description": "Show the recorded state of one mesh task by id (steps, result, status).",
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "Mesh task id."},
        },
        "required": ["task_id"],
    },
}

MESH_TAIL = {
    "name": "mesh_tail",
    "description": "Tail the live event log of one mesh task by id.",
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "Mesh task id."},
        },
        "required": ["task_id"],
    },
}
