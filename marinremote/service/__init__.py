"""Optional machine-installed input broker; the network and permissions stay in the user app."""

NAME = "MarinRemoteInput"
PIPE = r"\\.\pipe\MarinRemote.Input.v1"
AGENT_PREFIX = r"\\.\pipe\MarinRemote.Agent."
MAX_MESSAGE = 4096

