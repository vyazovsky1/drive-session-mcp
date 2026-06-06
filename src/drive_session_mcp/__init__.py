"""Browser-session Google Drive MCP server.

Reuses a persisted Playwright Chromium profile (authenticated once via corporate
SSO) to search and fetch Google Drive files headless and silently -- with no
OAuth2 client credentials and no GCP project.
"""

__version__ = "0.1.0"
