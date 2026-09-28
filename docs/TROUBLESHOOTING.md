# Troubleshooting

| Message or symptom | What to do |
|---|---|
| `uv` is not recognized / command not found | Install uv from its official instructions, then reopen the terminal. |
| `No pyproject.toml found` | Open the terminal in `engine/` inside the starter folder, or the source checkout root. Look for `pyproject.toml`. |
| First install downloads files | Expected: uv installs Python 3.12 and dependencies once. The bundled simulation itself is offline. |
| `output_folder_not_empty` | Choose a new folder, such as `--output outputs/demo-2`. Previous results are deliberately preserved. |
| Browser did not open | Double-click the printed `report.html` file. |
| HTML appears as source on GitHub | Download/extract the release and open the HTML file locally. |
| Windows path contains spaces | Quote paths: `--output "outputs/my test"`. |
| CSV data is rejected | Check the exact headers, UTC timezone, contiguous minute rows, OHLC values, and 24-hour warmup in the settings guide. |
| No trades appear | Read rejection reasons in the detailed report. Lack of entries can be a valid strategy result. |
| A model record is missing | This is not an API-key problem. Historical model outputs are unavailable unless original exact records were supplied. |
| A long test exceeds its budget | Shorten the dataset or use the advanced interfaces with a deliberate local resource plan. |

When asking for help, include your operating system, `uv --version`, `uv run --frozen liquid-lab --version`, the exact command, and the error. Remove private paths or source/account data. Do not post credentials. Use the repository's Issues page or your community support channel.
