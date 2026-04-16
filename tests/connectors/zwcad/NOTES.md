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
```

## Drawing Primitives

Point array helper (required for all geometry APIs):
```python
def pt3(x, y, z=0):
    return win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8,
                                   array.array('d', [x, y, z]))

def poly2d(coords):          # flat [x0,y0, x1,y1, ...]
    return win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8,
                                   array.array('d', coords))
```

Common calls:
```python
space.AddLine(pt3(0,0), pt3(10,0))
space.AddLightWeightPolyline(poly2d([0,0, 10,0, 10,5, 0,5, 0,0]))
space.AddCircle(pt3(5,5), 3.0)
doc.SendCommand("ZOOM E\n")   # fit view
doc.SendCommand("REGEN\n")
```

## Window Management

```python
import win32gui, win32con
win32gui.ShowWindow(zw.HWND, win32con.SW_SHOWMAXIMIZED)
# SetForegroundWindow may fail from background process; use ShowWindow instead
```

## Reading Entities

```python
for entity in space:
    print(entity.EntityName, entity.Layer)
    if entity.EntityName == "AcDbText":
        print(entity.TextString)
```

## Known Issues

- `GetActiveObject("ZwCAD.Application")` fails if ZWCAD hasn't registered in ROT yet — use `Dispatch` instead; it connects to an existing instance or launches a new one.
- Multiple `Dispatch` calls across exec sessions each launch a new ZWCAD instance. Check `tasklist /FI "IMAGENAME eq ZwCAD.exe"` and kill extras if needed.
- `SetForegroundWindow` raises error `(0, 'No error message')` when called from a background process without focus lock — use `ShowWindow(hwnd, SW_SHOWMAXIMIZED)` instead.
- Screenshot via PowerShell subprocess creates a visible console window unless launched with `creationflags=0x08000000` (CREATE_NO_WINDOW).
