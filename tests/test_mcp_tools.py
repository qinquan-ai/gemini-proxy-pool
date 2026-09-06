import unittest
from app.mcp_server import mcp

class TestMcpServerTools(unittest.TestCase):
    def test_tools_registered(self):
        # FastMCP registered tools
        tool_names = [t.name for t in mcp._tool_manager.list_tools()]
        print("Registered MCP tools:", tool_names)
        self.assertIn("analyze_image", tool_names)
        self.assertIn("generate_image", tool_names)
        self.assertIn("submit_video_analysis", tool_names)
        self.assertIn("get_video_analysis", tool_names)
        self.assertIn("cancel_video_analysis", tool_names)
        self.assertIn("analyze_video", tool_names)

if __name__ == "__main__":
    unittest.main()
