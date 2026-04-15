<!-- emerge:connector:zwcad — auto-generated at SessionStart -->
# Connector: zwcad

# ZWCAD Connector Notes

## COM Access

ProgID: `ZwCAD.Application`

```python
import pythoncom, win32com.client, array
pythoncom.CoInitialize()                                    # required every call
zw = win32com.client.Dispatch("ZwCAD.Application")
zw.Visible = True

# New document if none open
if zw.Documents.Count == 0:
    zw.Documents.Add()

doc = zw.ActiveDocument
space = doc.ModelSpace
`
