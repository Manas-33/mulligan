# Acme Widgets operations workspace

You work on Acme Widgets' systems (the production database, GitHub and the Notion docs) only through the `mulligan` MCP server's tools. There are no other credentials on this machine, and direct database, GitHub, Notion or gateway access from the shell is blocked.

Writes that can't be undone (emails, payments, tools from other servers) are held for a person's approval and come back with status `held`; carry on with the rest of the task.
