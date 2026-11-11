"""memnc plugin — push tasks into a memnc mesh from any Hermes agent."""

from . import schemas, tools


def register(ctx):
    """Register the mesh tools."""
    ctx.register_tool(name="mesh_submit", toolset="memnc",
                      schema=schemas.MESH_SUBMIT, handler=tools.mesh_submit)
    ctx.register_tool(name="mesh_status", toolset="memnc",
                      schema=schemas.MESH_STATUS, handler=tools.mesh_status)
    ctx.register_tool(name="mesh_state", toolset="memnc",
                      schema=schemas.MESH_STATE, handler=tools.mesh_state)
    ctx.register_tool(name="mesh_tail", toolset="memnc",
                      schema=schemas.MESH_TAIL, handler=tools.mesh_tail)
