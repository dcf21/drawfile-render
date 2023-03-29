# drawfile-render

The Python script `render_drawfile.py` in this repository converts Acorn / RISC OS !Draw files to PNG and SVG format. It can also produce a textual summary of the sequence of objects contained in a Drawfile.

The support for rendering bitmapped Sprite objects is based on David Boddie's `spritefile` module, which has been updated from its [original Python 2 implementation](https://gitlab.com/dboddie/spritefile/) to Python 3.

### Usage

```
./render_drawfile.py --input ${input_drawfile} --output ${output_png}
```

---

## Author

This code was developed by Dominic Ford <https://dcford.org.uk>. It is distributed under the Gnu General Public License V3.
