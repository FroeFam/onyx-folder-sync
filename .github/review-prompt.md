Repository onyx-folder-sync: a command line tool that walks local or mounted folders, extracts text from files and keeps an Onyx knowledge base in sync through the Onyx Ingestion API. The worst failure is deleting documents from Onyx that still exist on disk, so treat anything on the delete path as high risk.

Focus on:
- Correctness and potential bugs
- Delete safety: a document is deleted only when its file is proven absent, never because a folder was unreadable, a share was unmounted or a walk was incomplete
- The tool only ever deletes documents it created itself
- Security issues (path traversal out of a configured root, secrets in logs or state, unsafe handling of untrusted file content)
- Python idioms and best practices, full type annotations (mypy strict)
- Test coverage for new logic
- Breaking changes to the command line, the configuration format or the state database