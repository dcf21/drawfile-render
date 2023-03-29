#!/usr/bin/env python
"""
spr2img.py

Convert Acorn Spritefiles into other formats using the Python Imaging Library.
Spritefiles containing multiple images are split into many files which can be
stored in a named directory.

(C) David Boddie 2003-2005

This program is free software; you can redistribute it and/or
modify it under the terms of the GNU General Public License
as published by the Free Software Foundation; either version 2
of the License, or (at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program; if not, write to the Free Software
Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston,
MA  02110-1301, USA.
"""

import os
import re
from PIL import Image

suffix_sep = "."


def list_sprites(sf, spr):
    # Print information on the sprites.
    print()
    print('Spritefile "%s" contains:' % sf)
    print()

    if len(spr.sprites) > 0:

        for name in list(spr.sprites.keys()):
            print(name)

        print()

    return


def convert_sprites(spr, output_dir, format, scaling=4):
    # Convert each sprite to the format which uses the suffix given.
    for name, sprite in list(spr.sprites.items()):

        # Create an Image object.
        image = Image.frombytes(
            sprite['mode'], (sprite['width'], sprite['height']),
            bytes(sprite['image'])
        )

        image = image.resize(size=(sprite['width'] * scaling, sprite['height'] * scaling), resample=Image.NEAREST)

        # Write the image to a file in the output directory.
        path = "?"
        try:

            path = os.path.join(output_dir, re.sub("/", "_", name)) + suffix_sep + format
            fp = open(path, "wb")

            image.save(fp, format)
            fp.close()

        except IOError:

            print("Failed to open file for sprite: %s" % path)

        except KeyError:

            print("Could not convert sprite to format: %s" % format)
            break

    return
