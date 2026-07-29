# Reading Engine contracts

`ReadingEngineInterface` is the boundary for the Reading Engine. It defines
the operations that future application services or adapters may implement;
this module does not manage state, persistence, OCR, or reading behavior.

## Operations

| Operation | Purpose | Result |
| --- | --- | --- |
| `start_session()` | Establish a reading-session boundary | `Session` |
| `end_session()` | Close a reading-session boundary | `Session` |
| `update_page()` | Move the session page cursor | `ReadingState` |
| `store_ocr()` | Associate an OCR page with a session | `None` |
| `get_current_context()` | Read the active OCR page context | `OCRPage \| None` |
| `get_current_paragraph()` | Read the active paragraph | `str \| None` |
| `store_selected_word()` | Record a selected word | `None` |
| `get_session_summary()` | Retrieve session metadata | `Session` |

The interface uses shared models from `backend.app.models` and keeps all
storage and processing decisions outside this module.