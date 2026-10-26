"""Writes two-colour-test.3mf: a 50 x 50 x 2 mm tile for a first two-colour print.

A 1 mm base in colour 1 (left nozzle, extruder 1), and on top a 5 x 5 checkerboard of 10 mm squares,
alternating between extruder 1 and extruder 2 (right nozzle). Where the squares meet shows how well the
two nozzles line up: clean edges, or a gap / overlap along X or Y.

Every square is its own part, so no two pieces of one part touch along an edge. The file holds only the
model and which extruder each part uses, no printer settings: opening it keeps the presets selected in
PrusaSlicer. Run: python make_two_colour_test.py
"""
import os
import zipfile

SIZE, SQUARES, BASE, TOP = 50.0, 5, 1.0, 1.0
X0, Y0 = 65.0, 110.0                     # where it sits before Arrange (inside the 179 x 275 two-colour bed)


def box(x0, y0, z0, x1, y1, z1):
    v = [(x, y, z) for z in (z0, z1) for y in (y0, y1) for x in (x0, x1)]
    t = [(0, 2, 1), (1, 2, 3), (4, 5, 6), (5, 7, 6), (0, 1, 4), (1, 5, 4),
         (2, 6, 3), (3, 6, 7), (0, 4, 2), (2, 4, 6), (1, 3, 5), (3, 7, 5)]
    return v, t


parts = [("base", 1, box(X0, Y0, 0, X0 + SIZE, Y0 + SIZE, BASE))]
step = SIZE / SQUARES
for j in range(SQUARES):
    for i in range(SQUARES):
        x, y = X0 + i * step, Y0 + j * step
        parts.append((f"square {i + 1},{j + 1}", 1 if (i + j) % 2 == 0 else 2,
                      box(x, y, BASE, x + step, y + step, BASE + TOP)))

verts, tris, ranges = [], [], []
for name, ext, (v, t) in parts:
    base, first = len(verts), len(tris)
    verts += v
    tris += [(a + base, b + base, c + base) for a, b, c in t]
    ranges.append((name, ext, first, len(tris) - 1))

model = ['<?xml version="1.0" encoding="UTF-8"?>',
         '<model unit="millimeter" xml:lang="en-US" xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">',
         '<metadata name="Title">NORTHBOLT two-colour test</metadata>',
         "<resources>", '<object id="1" type="model">', "<mesh>", "<vertices>"]
model += [f'<vertex x="{x:g}" y="{y:g}" z="{z:g}"/>' for x, y, z in verts]
model += ["</vertices>", "<triangles>"]
model += [f'<triangle v1="{a}" v2="{b}" v3="{c}"/>' for a, b, c in tris]
model += ["</triangles>", "</mesh>", "</object>", "</resources>",
          "<build>", '<item objectid="1" transform="1 0 0 0 1 0 0 0 1 0 0 0" printable="1"/>', "</build>", "</model>"]
cfg = ['<?xml version="1.0" encoding="UTF-8"?>', "<config>", '<object id="1" instances_count="1">',
       '<metadata type="object" key="name" value="two-colour test"/>']
for name, ext, first, last in ranges:
    cfg += [f'<volume firstid="{first}" lastid="{last}">',
            f'<metadata type="volume" key="name" value="{name}"/>',
            f'<metadata type="volume" key="extruder" value="{ext}"/>', "</volume>"]
cfg += ["</object>", "</config>"]
types = ('<?xml version="1.0" encoding="UTF-8"?>\n<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
         '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
         '<Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/></Types>')
rels = ('<?xml version="1.0" encoding="UTF-8"?>\n<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Target="/3D/3dmodel.model" Id="rel-1" Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>'
        "</Relationships>")
out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "two-colour-test.3mf")
with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
    z.writestr("[Content_Types].xml", types)
    z.writestr("_rels/.rels", rels)
    z.writestr("3D/3dmodel.model", "\n".join(model))
    z.writestr("Metadata/Slic3r_PE_model.config", "\n".join(cfg))
print("wrote", out, f"({len(parts)} parts)")
