#!/usr/bin/env python

# spritefile.py
# A module for reading and writing Acorn Spritefiles.
#
# (C) David Boddie 2001-2005
# 
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; either version 2
# of the License, or (at your option) any later version.
# 
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
# 
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston,
# MA  02110-1301, USA.

"""
    spritefile.py

    Read a Sprite file and store the contents in an instance of a Sprite
    file class.
"""

version = '0.22'

import string
import struct


class spritefile_error(Exception):
    pass


scale8 = 255.0 / 15.0  # scaling factor for 8 bits per pixel colour
# components
# scale8  = 254.0/16.0       # scaling factor for 8 bits per pixel colour
# components
scale16 = 255.0 / 31.0  # scaling factor for 16 bits per pixel colour


# components

# ---------------------------------------------------------------------------
# Fast block decoders
#
# The original sprite2rgb / mask2byte / mask2rgba methods decode one pixel at a
# time with a file.seek() + file.read(1) per pixel inside a doubly-nested
# Python loop.  On a large image (a scanned A4 page at 300 dpi is ~8.7M pixels)
# this is extremely slow.  The helpers below let those methods read the whole
# image/mask region in a single file.read() and decode it with precomputed
# lookup tables and bytes operations, which is one to two orders of magnitude
# faster while producing byte-identical output.  They use only the standard
# library.
# ---------------------------------------------------------------------------

# LUT caches, keyed by the scaling constant so a change to scale8/scale16 is
# reflected rather than served stale.
_VIDC256_CACHE = {}
_RGB565_CACHE = {}


def _vidc256_colour_lut(scale8):
    """List of 256 3-byte RGB values for the standard VIDC 8-bit palette."""
    lut = _VIDC256_CACHE.get(scale8)
    if lut is None:
        lut = []
        for v in range(256):
            red = ((v & 0x10) >> 1) | (v & 7)
            green = ((v & 0x40) >> 3) | ((v & 0x20) >> 3) | (v & 3)
            blue = ((v & 0x80) >> 4) | ((v & 8) >> 1) | (v & 3)
            lut.append(bytes((int(red * scale8), int(green * scale8), int(blue * scale8))))
        _VIDC256_CACHE[scale8] = lut
    return lut


def _rgb565_lut(scale16):
    """List of 65536 3-byte RGB values for 16-bit (5:5:5) sprite pixels."""
    lut = _RGB565_CACHE.get(scale16)
    if lut is None:
        lut = []
        for value in range(65536):
            red = int((value & 0x1f) * scale16)
            green = int(((value >> 5) & 0x1f) * scale16)
            blue = int(((value >> 10) & 0x1f) * scale16)
            lut.append(bytes((red, green, blue)))
        _RGB565_CACHE[scale16] = lut
    return lut


def _colour_lut(palette, count):
    """Build a colour LUT (list of 3-byte RGB) from a palette, mirroring the
    scalar code's palette[value][0] indexing.  Returns None if the palette is
    too short for `count` entries."""
    if len(palette) < count:
        return None
    return [bytes(palette[i][0]) for i in range(len(palette))]


def _byte_expand_table(bpp, colour_lut):
    """Map each of 256 input byte values to its expanded RGB output bytes.

    A source byte holds 8 // bpp LSB-first pixels (bpp in 1, 2, 4).  Returns a
    list of 256 bytes objects, or None if a pixel value is outside colour_lut."""
    ppb = 8 // bpp
    mask = (1 << bpp) - 1
    if len(colour_lut) < (1 << bpp):
        return None
    table = []
    for v in range(256):
        out = bytearray()
        for k in range(ppb):
            out += colour_lut[(v >> (k * bpp)) & mask]
        table.append(bytes(out))
    return table


def _mask_geometry(width, bpp):
    """Return (effective_bpp, row_stride_bytes) for a mask, matching the scalar
    code (32/16 bpp masks are stored 1bpp)."""
    if bpp == 32 or bpp == 16:
        bpp = 1
    bits = bpp * width
    row_size = bits >> 5
    if bits % 32 != 0:
        row_size += 1
    return bpp, row_size * 4


def _alpha_expand_table(bpp):
    """Map each of 256 input byte values to its expanded mask-alpha bytes,
    mirroring the scalar mask logic.  Returns None for bpp 8 (identity)."""
    if bpp == 8:
        return None
    ppb = 8 // bpp
    mask = (1 << bpp) - 1
    table = []
    for v in range(256):
        out = bytearray()
        for k in range(ppb):
            value = (v >> (k * bpp)) & mask
            if bpp == 4:
                value = value | (value << 4)
            elif bpp == 2:
                value = 0xff if value == 3 else value
            elif bpp == 1:
                value = value * 0xff
            out.append(value)
        table.append(bytes(out))
    return table


def _decode_mask_alpha(block, byte_offset, row_stride, eff_bpp, width, height):
    """Decode a mask block to flat alpha bytes (length width*height), or None."""
    alpha = bytearray()
    if eff_bpp == 8:
        for j in range(height):
            base = j * row_stride + byte_offset
            row = block[base:base + width]
            if len(row) < width:
                return None
            alpha += row
        return alpha
    table = _alpha_expand_table(eff_bpp)
    if table is None:
        return None
    ppb = 8 // eff_bpp
    n_src = (width + ppb - 1) // ppb
    for j in range(height):
        base = j * row_stride + byte_offset
        row = block[base:base + n_src]
        if len(row) < n_src:
            return None
        alpha += b''.join([table[b] for b in row])[:width]
    return alpha



class spritefile:
    """spritefile
    
    sprite_file = spritefile(file = None)
    
    Create a spritefile object, either empty or based on the contents of a
    file. The file parameter should be either a file object or at least have
    file object style methods for reading and seeking.
    """

    def __init__(self, file=None):

        # Constants
        self.HEADER = 60

        # Mode information dictionary (log2bpp, x scale, y scale)
        self.mode_info = {
            0: (0, 1, 2), 1: (1, 2, 2), 2: (2, 3, 2), 3: (1, 1, 2),
            4: (0, 2, 2), 5: (1, 3, 2), 6: (1, 2, 2), 7: (2, 2, 2),
            8: (1, 1, 2), 9: (2, 2, 2), 10: (3, 3, 2), 11: (1, 1, 2),
            12: (2, 1, 2), 13: (3, 2, 2), 14: (2, 1, 2), 15: (3, 1, 2),
            16: (2, 1, 2), 17: (2, 1, 2), 18: (0, 1, 1), 19: (1, 1, 1),
            20: (2, 1, 1), 21: (3, 1, 1), 22: (2, 0, 1), 23: (0, 1, 1),
            24: (3, 1, 2), 25: (0, 1, 1), 26: (1, 1, 1), 27: (2, 1, 1),
            28: (3, 1, 1), 29: (0, 1, 1), 30: (1, 1, 1), 31: (2, 1, 1),
            32: (3, 1, 1), 33: (0, 1, 2), 34: (1, 1, 2), 35: (2, 1, 2),
            36: (3, 1, 2), 37: (0, 1, 2), 38: (1, 1, 2), 39: (2, 1, 2),
            40: (3, 1, 2), 41: (0, 1, 2), 42: (1, 1, 2), 43: (2, 1, 2),
            44: (0, 1, 2), 45: (1, 1, 2), 46: (2, 1, 2), 47: (3, 2, 2),
            48: (2, 2, 1), 49: (3, 2, 1)
        }

        # self.palette16 = {
        #           0:  (0xff, 0xff, 0xff), 1 : (0xdd, 0xdd, 0xdd),
        #           2:  (0xbb, 0xbb, 0xbb), 3 : (0x99, 0x99, 0x99),
        #           4:  (0x77, 0x77, 0x77), 5 : (0x55, 0x55, 0x55),
        #           6:  (0x33, 0x33, 0x33), 7 : (0x00, 0x00, 0x00),
        #           8:  (0x00, 0x44, 0x99), 9 : (0xee, 0xee, 0x00),
        #           10: (0x00, 0xcc, 0x00), 11: (0xdd, 0x00, 0x00),
        #           12: (0xee, 0xee, 0xbb), 13: (0x55, 0x88, 0x00),
        #           14: (0xff, 0xbb, 0x00), 15: (0x00, 0xbb, 0xff)
        #       }
        #
        # self.palette4 = {
        #           0: (0xff, 0xff, 0xff), 1: (0xbb, 0xbb, 0xbb),
        #           2: (0x77, 0x77, 0x77), 3: (0x00, 0x00, 0x00)
        #       }

        self.palette16 = [
            (0xff, 0xff, 0xff), (0xdd, 0xdd, 0xdd),
            (0xbb, 0xbb, 0xbb), (0x99, 0x99, 0x99),
            (0x77, 0x77, 0x77), (0x55, 0x55, 0x55),
            (0x33, 0x33, 0x33), (0x00, 0x00, 0x00),
            (0x00, 0x44, 0x99), (0xee, 0xee, 0x00),
            (0x00, 0xcc, 0x00), (0xdd, 0x00, 0x00),
            (0xee, 0xee, 0xbb), (0x55, 0x88, 0x00),
            (0xff, 0xbb, 0x00), (0x00, 0xbb, 0xff)
        ]

        self.palette4 = [
            (0xff, 0xff, 0xff), (0xbb, 0xbb, 0xbb),
            (0x77, 0x77, 0x77), (0x00, 0x00, 0x00)
        ]

        if file != None:
            self.read(file)
        else:
            self.new()

    def new(self):

        self.sprites = {}
        self.warnings = []

    def number(self, size, n):

        # Little endian writing

        s = bytearray()

        while size > 0:
            i = n % 256
            s.append(i)
            #           n = n / 256
            n = n >> 8
            size = size - 1

        return s

    def str2num(self, size, s):

        if len(s) < size:
            raise spritefile_error(
                'Unexpected end of file: expected %d bytes, got %d.' % (size, len(s))
            )

        i = 0
        n = 0
        while i < size:
            n = n | (s[i] << (i * 8))
            i = i + 1

        return n

    def sprite2rgb(self, file, width, height, h_words, first_bit_used, bpp,
                   palette):

        # Fast path: decode the whole image in one read using lookup tables.
        # Falls back to the per-pixel scalar decoder for anything it does not
        # handle (non-byte-aligned offsets, short palettes, truncated data).
        start = file.tell()
        out = self._sprite2rgb_fast(file, width, height, h_words,
                                    first_bit_used, bpp, palette)
        if out is not None:
            return out
        file.seek(start, 0)
        return self._sprite2rgb_scalar(file, width, height, h_words,
                                       first_bit_used, bpp, palette)

    def _sprite2rgb_fast(self, file, width, height, h_words, first_bit_used,
                         bpp, palette):

        if (first_bit_used & 7) != 0:
            return None

        byte_offset = first_bit_used >> 3
        row_stride = h_words * 4
        block = file.read(height * row_stride)
        if len(block) < height * row_stride:
            return None

        has_palette = palette != []
        rgb = bytearray()

        if bpp == 32:
            need = width * 4
            for j in range(height):
                base = j * row_stride + byte_offset
                row = bytearray(block[base:base + need])
                if len(row) < need:
                    return None
                del row[3::4]                       # drop the unused 4th byte
                rgb += row

        elif bpp == 16:
            lut = _rgb565_lut(scale16)
            need = width * 2
            for j in range(height):
                base = j * row_stride + byte_offset
                row = block[base:base + need]
                if len(row) < need:
                    return None
                rgb += b''.join([lut[v] for v in struct.unpack('<%dH' % width, row)])

        elif bpp == 8:
            if has_palette:
                colour = _colour_lut(palette, 256)
            else:
                colour = _vidc256_colour_lut(scale8)
            if colour is None:
                return None
            maxval = len(colour)
            for j in range(height):
                base = j * row_stride + byte_offset
                row = block[base:base + width]
                if len(row) < width or (row and max(row) >= maxval):
                    return None
                rgb += b''.join([colour[b] for b in row])

        elif bpp in (4, 2, 1):
            if has_palette:
                colour = _colour_lut(palette, 1 << bpp)
            elif bpp == 4:
                colour = [bytes(c) for c in self.palette16]
            elif bpp == 2:
                colour = [bytes(c) for c in self.palette4]
            else:
                colour = [b'\xff\xff\xff', b'\x00\x00\x00']
            table = _byte_expand_table(bpp, colour) if colour is not None else None
            if table is None:
                return None
            ppb = 8 // bpp
            n_src = (width + ppb - 1) // ppb
            out_len = width * 3
            for j in range(height):
                base = j * row_stride + byte_offset
                row = block[base:base + n_src]
                if len(row) < n_src:
                    return None
                rgb += b''.join([table[b] for b in row])[:out_len]

        else:
            return None

        return rgb

    def _sprite2rgb_scalar(self, file, width, height, h_words, first_bit_used, bpp,
                   palette):

        # Convert sprite to RGB values

        if palette != []:
            has_palette = 1
        else:
            has_palette = 0

        rgb = bytearray()
        ptr = file.tell() * 8  # bit offset

        for j in range(0, height):

            row = bytearray()
            row_ptr = ptr + first_bit_used  # bit offset into the image

            for i in range(0, width):

                file.seek(row_ptr >> 3, 0)

                # Conversion depends on bpp value
                if bpp == 32:

                    red = file.read(1)[0]
                    green = file.read(1)[0]
                    blue = file.read(1)[0]
                    row_ptr = row_ptr + 32

                elif bpp == 16:

                    value = self.str2num(2, file.read(2))
                    red = int((value & 0x1f) * scale16)
                    green = int(((value >> 5) & 0x1f) * scale16)
                    blue = int(((value >> 10) & 0x1f) * scale16)
                    row_ptr = row_ptr + 16

                elif bpp == 8:

                    if has_palette == 0:

                        # Standard VIDC 256 colours
                        value = file.read(1)[0]
                        red = ((value & 0x10) >> 1) | (value & 7)
                        green = ((value & 0x40) >> 3) | \
                                ((value & 0x20) >> 3) | (value & 3)
                        blue = ((value & 0x80) >> 4) | \
                               ((value & 8) >> 1) | (value & 3)
                        red = int(red * scale8)
                        green = int(green * scale8)
                        blue = int(blue * scale8)
                    else:
                        # 256 entry palette
                        value = file.read(1)[0]
                        red, green, blue = palette[value][0]

                    row_ptr = row_ptr + 8

                elif bpp == 4:

                    value = (file.read(1)[0] >> (row_ptr % 8)) & 0xf

                    if has_palette == 0:

                        # Standard 16 desktop colours
                        # Look up the value in the standard palette
                        red, green, blue = self.palette16[value]
                    else:
                        # 16 entry palette
                        red, green, blue = palette[value][0]

                    row_ptr = row_ptr + 4

                elif bpp == 2:

                    value = (file.read(1)[0] >> (row_ptr % 8)) & 0x3

                    if has_palette == 0:

                        # Greyscales
                        red, green, blue = self.palette4[value]
                    else:
                        # 4 entry palette
                        red, green, blue = palette[value][0]

                    row_ptr = row_ptr + 2

                elif bpp == 1:

                    value = (file.read(1)[0] >> (row_ptr % 8)) & 1

                    if has_palette == 0:

                        # Black and white
                        red = green = blue = (255 * (1 - value))
                    else:
                        # 2 entry palette
                        red, green, blue = palette[value][0]

                    row_ptr = row_ptr + 1

                row.append(red)
                row.append(green)
                row.append(blue)

            rgb = rgb + row
            ptr = ptr + (h_words * 32)

        return rgb

    def sprite2cmyk(self, file, width, height, h_words):

        # Read a CMYK sprite.

        ptr = file.tell() * 8  # bit offset
        """
        cmyk = []

        for j in range(0, height):

            row = []

            for i in range(0, width):

                file.seek(row_ptr >> 3, 0)

                # Conversion depends on bpp value (32 bpp in this case).

                cyan = file.read(1)[0]
                magenta = file.read(1)[0]
                yellow = file.read(1)[0]
                key = file.read(1)[0]

                row.append('%s%s%s%s' % ())

            cmyk = cmyk + row
            ptr = ptr + (h_words * 32)
        """
        return file.read(width * height * 4)
        # return string.join(cmyk, '')

    def mask2byte(self, file, width, height, bpp):

        start = file.tell()
        eff_bpp, row_stride = _mask_geometry(width, bpp)
        block = file.read(height * row_stride)
        if len(block) >= height * row_stride:
            alpha = _decode_mask_alpha(block, 0, row_stride, eff_bpp, width, height)
            if alpha is not None:
                return alpha
        file.seek(start, 0)
        return self._mask2byte_scalar(file, width, height, bpp)

    def _mask2byte_scalar(self, file, width, height, bpp):

        mask = bytearray()

        ptr = file.tell() * 8  # bit offset
        image_ptr = 0

        if bpp == 32 or bpp == 16:
            bpp = 1

        # Colour depths below 16 bpp have the same number of bpp in the mask
        bits = bpp * width

        row_size = bits >> 5  # number of words
        if bits % 32 != 0:
            row_size = row_size + 1

        for j in range(0, height):

            row = bytearray()
            row_ptr = ptr  # bit offset into the image

            for i in range(0, width):

                file.seek(row_ptr >> 3, 0)

                # Conversion depends on bpp value
                if bpp == 32:

                    value = (file.read(1)[0] >> (row_ptr % 8)) & 1
                    value = value * 0xff
                    row_ptr = row_ptr + 1

                elif bpp == 16:

                    value = (file.read(1)[0] >> (row_ptr % 8)) & 1
                    value = value * 0xff
                    row_ptr = row_ptr + 1

                elif bpp == 8:

                    value = file.read(1)[0]
                    row_ptr = row_ptr + 8

                elif bpp == 4:

                    value = (file.read(1)[0] >> (row_ptr % 8)) & 0xf
                    value = value | (value << 4)
                    row_ptr = row_ptr + 4

                elif bpp == 2:

                    value = (file.read(1)[0] >> (row_ptr % 8)) & 0x3
                    if value == 3:
                        value = 0xff
                    row_ptr = row_ptr + 2

                elif bpp == 1:

                    # Black and white
                    value = (file.read(1)[0] >> (row_ptr % 8)) & 1
                    value = value * 0xff
                    row_ptr = row_ptr + 1

                row.append(value)

            mask = mask + row
            ptr = ptr + (row_size * 32)

        return mask

    def mask2rgba(self, file, width, height, first_bit_used, bpp, image):

        start = file.tell()
        if (first_bit_used & 7) == 0 and len(image) >= width * height * 3:
            byte_offset = first_bit_used >> 3
            eff_bpp, row_stride = _mask_geometry(width, bpp)
            block = file.read(height * row_stride)
            if len(block) >= height * row_stride:
                alpha = _decode_mask_alpha(block, byte_offset, row_stride,
                                           eff_bpp, width, height)
                if alpha is not None:
                    rgb = bytes(image)
                    n = width * height
                    rgba = bytearray(n * 4)
                    rgba[0::4] = rgb[0:n * 3:3]
                    rgba[1::4] = rgb[1:n * 3:3]
                    rgba[2::4] = rgb[2:n * 3:3]
                    rgba[3::4] = alpha
                    return rgba
        file.seek(start, 0)
        return self._mask2rgba_scalar(file, width, height, first_bit_used, bpp, image)

    def _mask2rgba_scalar(self, file, width, height, first_bit_used, bpp, image):

        rgba = bytearray()

        ptr = file.tell() * 8  # bit offset
        image_ptr = 0

        if bpp == 32 or bpp == 16:
            bpp = 1

        # Colour depths below 16 bpp have the same number of bpp in the mask
        bits = bpp * width

        row_size = bits >> 5  # number of words
        if bits % 32 != 0:
            row_size = row_size + 1

        for j in range(0, height):

            row = bytearray()
            row_ptr = ptr + first_bit_used  # bit offset into the image

            for i in range(0, width):

                file.seek(row_ptr >> 3, 0)

                # Conversion depends on bpp value
                if bpp == 32:

                    value = (file.read(1)[0] >> (row_ptr % 8)) & 1
                    value = value * 0xff
                    row_ptr = row_ptr + 1

                elif bpp == 16:

                    value = (file.read(1)[0] >> (row_ptr % 8)) & 1
                    value = value * 0xff
                    row_ptr = row_ptr + 1

                elif bpp == 8:

                    value = file.read(1)[0]
                    row_ptr = row_ptr + 8

                elif bpp == 4:

                    value = (file.read(1)[0] >> (row_ptr % 8)) & 0xf
                    value = value | (value << 4)
                    row_ptr = row_ptr + 4

                elif bpp == 2:

                    value = (file.read(1)[0] >> (row_ptr % 8)) & 0x3
                    if value == 3:
                        value = 0xff
                    row_ptr = row_ptr + 2

                elif bpp == 1:

                    # Black and white
                    value = (file.read(1)[0] >> (row_ptr % 8)) & 1
                    value = value * 0xff
                    row_ptr = row_ptr + 1

                row.append(image[image_ptr])
                row.append(image[image_ptr+1])
                row.append(image[image_ptr+2])
                row.append(value)
                image_ptr = image_ptr + 3

            rgba = rgba + row
            ptr = ptr + (row_size * 32)

        return rgba

    def read_details(self, file, offset):

        # Go to start of this sprite
        file.seek(offset, 0)

        next = self.str2num(4, file.read(4))

        # We will return a data dictionary
        data = {}

        n = file.read(12)
        name = ''
        for i in n:
            if i > 32:
                name = name + chr(i)
            else:
                break

        # Read width of sprite in words and height in scan lines.
        # These is stored in the Spritefile as width-1 and height-1.
        h_words = self.str2num(4, file.read(4)) + 1
        v_lines = self.str2num(4, file.read(4)) + 1

        data['h_words'] = h_words
        data['v_lines'] = v_lines

        # The bits used in each word.
        first_bit_used = self.str2num(4, file.read(4))
        last_bit_used = self.str2num(4, file.read(4))

        data['first bit'] = first_bit_used
        data['last bit'] = last_bit_used

        # The pointers to the image and mask are found from the offsets
        # relative to the start of the sprite; i.e. from the next sprite
        # offset.
        image_ptr = offset + self.str2num(4, file.read(4))
        mask_ptr = offset + self.str2num(4, file.read(4))

        # The mode number of the sprite.
        mode = self.str2num(4, file.read(4))

        bpp = (mode >> 27)
        new_format_sprite = (bpp != 0)

        if bpp == 0:

            mode = mode & 0x3f

            # Information on commonly used modes
            if mode in self.mode_info:

                log2bpp, xscale, yscale = self.mode_info[mode]
                # xdpi = int(180/xscale)      # was 90
                # ydpi = int(180/yscale)      # was 90
                xdpi = int(90 / xscale)  # Old modes have a maximum of
                ydpi = int(90 / yscale)  # 90 dots per inch.
                bpp = 1 << log2bpp

                # Sprites for old screen modes are all converted to RGB format.
                data['mode'] = 'RGB'

            else:
                raise spritefile_error('Unknown mode number.')

        else:

            # The bits per pixel are read as follows.
            if bpp == 1:
                log2bpp = 0
                data['mode'] = 'RGB'
            elif bpp == 2:
                log2bpp = 1
                data['mode'] = 'RGB'
            elif bpp == 3:
                bpp = 4
                log2bpp = 2
                data['mode'] = 'RGB'
            elif bpp == 4:
                bpp = 8
                log2bpp = 3
                data['mode'] = 'RGB'
            elif bpp == 5:
                bpp = 16
                log2bpp = 4
                data['mode'] = 'RGB'
            elif bpp == 6:
                bpp = 32
                log2bpp = 5
                data['mode'] = 'RGB'
            elif bpp == 7:
                bpp = 32
                log2bpp = 5
                data['mode'] = 'CMYK'
            else:
                raise spritefile_error('Unknown number of bits per pixel.')

            xdpi = ((mode >> 1) & 0x1fff)
            ydpi = ((mode >> 14) & 0x1fff)

        data['bpp'] = bpp
        data['log2bpp'] = log2bpp
        data['dpi x'] = xdpi
        data['dpi y'] = ydpi

        has_palette = 0

        palette = []

        # Read palette, if present, putting the values into a list
        while file.tell() < image_ptr:
            file.seek(1, 1)  # skip a byte
            # First entry (red, green, blue)
            entry1 = (file.read(1)[0], file.read(1)[0], file.read(1)[0])
            file.seek(1, 1)  # skip a byte
            # Second entry (red, green, blue)
            entry2 = (file.read(1)[0], file.read(1)[0], file.read(1)[0])
            palette.append((entry1, entry2))

        if palette != []:

            if bpp == 8 and len(palette) < 256:

                if len(palette) == 16:

                    # Each four pairs of entries describes the variation
                    # in a particular colour: 0-3, 4-7, 8-11, 12-15
                    # These sixteen colours describe the rest of the 256
                    # colours.

                    for j in range(16, 256, 16):

                        for i in range(0, 16):
                            entry1, entry2 = palette[i]

                            # Generate new colours using the palette
                            # supplied for the first 16 colours
                            red = (((j + i) & 0x10) >> 1) | (entry1[0] >> 4)
                            green = (((j + i) & 0x40) >> 3) | \
                                    (((j + i) & 0x20) >> 3) | (entry1[1] >> 4)
                            blue = (((j + i) & 0x80) >> 4) | (entry1[2] >> 4)
                            red = int(red * scale8)
                            green = int(green * scale8)
                            blue = int(blue * scale8)

                            # Append new entries
                            palette.append(
                                ((red, green, blue), (red, green, blue)))

                elif len(palette) == 64:

                    for j in range(64, 256, 64):

                        for i in range(0, 64):
                            entry1, entry2 = palette[i]

                            red = (((j + i) & 0x10) >> 1) | (entry1[0] >> 4)
                            green = (((j + i) & 0x40) >> 3) | \
                                    (((j + i) & 0x20) >> 3) | (entry1[1] >> 4)
                            blue = (((j + i) & 0x80) >> 4) | (entry1[2] >> 4)
                            red = int(red * scale8)
                            green = int(green * scale8)
                            blue = int(blue * scale8)

                            # Append new entries
                            palette.append(
                                ((red, green, blue), (red, green, blue)))

                data['palette'] = palette
            else:
                data['palette'] = palette

        # The width of the sprite is the number of words used divided by the
        # bits per pixel of the sprite. Additionally, the parts of the sprite
        # unused at the ends are subtracted.
        width = (h_words * (32 >> log2bpp)) - (first_bit_used >> log2bpp) - \
                ((31 - last_bit_used) >> log2bpp)
        height = v_lines

        data['width'] = width
        data['height'] = height

        # Obtain image data
        file.seek(image_ptr, 0)

        if data['mode'] == 'RGB':
            data['image'] = self.sprite2rgb(file, width, height, h_words,
                                            first_bit_used, bpp, palette)

        elif data['mode'] == 'CMYK':

            data['image'] = self.sprite2cmyk(file, width, height, h_words)

        # Obtain mask data
        if mask_ptr != image_ptr:
            # Guard: only attempt mask processing if mask_ptr is within
            # the actual file. Some tools write mask_ptr = image_ptr +
            # image_data_size as a sentinel for "no mask", leaving nothing
            # at that offset. Seeking past EOF succeeds silently, but
            # file.read(1) then returns b'' causing IndexError in mask2rgba.
            file.seek(0, 2)           # seek to end to find file size
            file_size = file.tell()
            if mask_ptr < file_size:
                file.seek(mask_ptr, 0)

                # New format sprites always use 1bpp masks;
                # old format sprites use the same bpp as the image.
                mask_bpp = 1 if new_format_sprite else bpp
                data['image'] = self.mask2rgba(
                    file, width, height, first_bit_used, mask_bpp, data['image']
                )

                # The image is stored in RGBA form.
                data['mode'] = 'RGBA'

        return name, data, next

    def read(self, file):

        # Minimum valid sprite record: next(4) + name(12) + h_words(4) +
        # v_lines(4) + first_bit(4) + last_bit(4) + image_off(4) +
        # mask_off(4) + mode(4) = 44 bytes.
        MIN_SPRITE_HEADER = 44

        file.seek(0, 2)
        size = file.tell()
        file.seek(0, 0)

        # Examine the sprites
        number = self.str2num(4, file.read(4))
        offset = self.str2num(4, file.read(4)) - 4
        free = self.str2num(4, file.read(4)) - 4

        self.sprites = {}
        self.warnings = []

        while (offset < free):
            name, data, next = self.read_details(file, offset)

            self.sprites[name] = data

            if next == 0:
                # next == 0 is not a valid offset but the sprite itself is
                # intact; treat as a quiet end-of-chain terminator.
                break
            if next < MIN_SPRITE_HEADER:
                self.warnings.append(
                    'Corrupt sprite area: next-sprite offset %d is below minimum header size %d '
                    '(at file offset %d); stopping after %d sprite(s).'
                    % (next, MIN_SPRITE_HEADER, offset, len(self.sprites))
                )
                break
            if offset + next > free:
                self.warnings.append(
                    'Corrupt sprite area: next-sprite offset %d steps beyond free pointer '
                    '(at file offset %d, free = %d); stopping after %d sprite(s).'
                    % (next, offset, free, len(self.sprites))
                )
                break

            offset = offset + next

    def rgb2sprite(self, name):

        data = self.sprites[name]

        # Number of bits per pixel in the original sprite
        bpp = data['bpp']

        # If the sprite didn't have a palette then use a standard one
        if 'palette' in data:

            # Explicitly defined palette
            has_palette = 1
            palette = data['palette']

        else:
            # Standard palette - invert the built in palette
            if bpp == 4:

                palette = self.palette16

            elif bpp == 2:

                palette = self.palette4

            else:
                palette = []

            # There is no explicitly defined palette
            has_palette = 0

        # Image data
        image = data['image']

        # Storage mode: RGB or RGBA
        mode = data['mode']

        # Sprite and mask strings
        sprite = bytearray()
        mask = bytearray()

        # If there was either a palette specified or a standard one used
        # then create an inverse.
        if palette != []:

            # Create inverse palette dictionary
            inverse = {}

            for i in range(0, len(palette)):

                # There may be a list of two tuples in each palette entry.
                if type(palette[i][0]) != type(0):
                    inverse[palette[i][0]] = i
                    inverse[palette[i][1]] = i
                else:
                    # There may just be one tuple (for standard palettes).
                    inverse[palette[i]] = i

            # Store the inverse palette for convenience.
            self.sprites[name]['inverse'] = inverse

        # Write the image data to the sprite and mask
        ptr = 0

        for j in range(0, data['height']):

            sprite_word = 0
            mask_word = 0
            sprite_ptr = 0
            mask_ptr = 0

            for i in range(0, data['width']):

                # Read the red, green and blue components
                r = image[ptr]
                g = image[ptr + 1]
                b = image[ptr + 2]

                if mode == 'RGBA':

                    a = image[ptr + 3]
                    ptr = ptr + 4
                else:
                    # No alpha component
                    ptr = ptr + 3

                # Write the pixels to the sprite and mask
                if bpp == 32:

                    # Store the components in the sprite word
                    sprite_word = r | (g << 8) | (b << 16)  # was b << 24 !
                    sprite_ptr = 32

                    # Store mask data if relevant
                    if mode == 'RGBA':
                        mask_word = mask_word | ((a == 255) << mask_ptr)
                        mask_ptr = mask_ptr + 1

                elif bpp == 16:

                    # Convert the components to the relevant form
                    half_word = int(r / scale16) | \
                                (int(g / scale16) << 5) | \
                                (int(b / scale16) << 10)

                    sprite_word = sprite_word | (half_word << sprite_ptr)
                    sprite_ptr = sprite_ptr + 16

                    # Store mask data if relevant
                    if mode == 'RGBA':
                        mask_word = mask_word | ((a == 255) << mask_ptr)
                        mask_ptr = mask_ptr + 1

                elif bpp == 8:

                    # If there is a palette then look up the colour index
                    # in the inverse palette dictionary
                    if palette != []:

                        index = inverse[(r, g, b)]
                    else:
                        # Standard palette
                        red = int(r / scale8)
                        green = int(g / scale8)
                        blue = int(b / scale8)

                        index = ((red & 0x8) << 1) | (red & 0x4) | \
                                ((green & 0x8) << 3) | ((green & 0x4) << 3) | \
                                ((blue & 0x8) << 4) | ((blue & 0x4) << 1) | \
                                int((red + green + blue) / 15.0)

                    # Store the contents in the sprite word
                    sprite_word = sprite_word | (index << sprite_ptr)
                    sprite_ptr = sprite_ptr + 8

                    # Store mask data
                    if mode == 'RGBA':

                        if a != 0xff:
                            a = 0

                        mask_word = mask_word | (a << mask_ptr)
                        mask_ptr = mask_ptr + 8

                elif bpp == 4:

                    # Look up bit state in inverse palette
                    index = inverse[(r, g, b)]

                    # Store the contents in the sprite word
                    sprite_word = sprite_word | (index << sprite_ptr)
                    sprite_ptr = sprite_ptr + 4

                    # Store mask data
                    if mode == 'RGBA':

                        if a == 0xff:
                            a = 0xf
                        else:
                            a = 0

                        mask_word = mask_word | (a << mask_ptr)
                        mask_ptr = mask_ptr + 4

                elif bpp == 2:

                    # Look up bit state in inverse palette
                    index = inverse[(r, g, b)]

                    # Store the contents in the sprite word
                    sprite_word = sprite_word | (index << sprite_ptr)
                    sprite_ptr = sprite_ptr + 2

                    # Store mask data
                    if mode == 'RGBA':

                        if a == 0xff:
                            a = 0x3
                        else:
                            a = 0

                        mask_word = mask_word | (a << mask_ptr)
                        mask_ptr = mask_ptr + 2

                elif bpp == 1:

                    if palette != []:

                        # Look up bit state in inverse palette
                        bit = inverse[(r, g, b)]
                    else:
                        # Use red component
                        bit = (r == 255)

                    # Append bit to byte
                    sprite_word = sprite_word | (bit << sprite_ptr)
                    sprite_ptr = sprite_ptr + 1

                    # Determine mask bit if present
                    if mode == 'RGBA':
                        mask_word = mask_word | ((a == 255) << mask_ptr)
                        mask_ptr = mask_ptr + 1

                # Write the sprite word to the sprite string if the word is
                # full
                if sprite_ptr == 32:
                    # End of word, so reset offset,
                    sprite_ptr = 0
                    # store the word in the sprite string
                    sprite = sprite + self.number(4, sprite_word)
                    # and reset the byte
                    sprite_word = 0

                # Do the same for the mask
                if mask_ptr == 32:
                    mask_ptr = 0
                    mask = mask + self.number(4, mask_word)
                    mask_word = 0

            # Write any remaining sprite data to the sprite string
            if sprite_ptr > 0:
                # store the word in the sprite string
                sprite = sprite + self.number(4, sprite_word)

            # Do the same for the mask
            if mask_ptr > 0:
                mask = mask + self.number(4, mask_word)

        # Determine the actual number of words used per line of
        # the sprite.

        # The number of bits in the line.
        width_bits = data['width'] * bpp
        # The excess number of bits (not filling a word completely).
        excess_bits = width_bits % 32
        # The number of excess words is therefore either one or zero.
        excess_words = excess_bits != 0
        # The number of words used.
        width = int(width_bits / 32) + excess_words

        self.sprites[name]['h_words'] = width
        self.sprites[name]['v_lines'] = data['height']

        self.sprites[name]['first bit'] = 0
        self.sprites[name]['last bit'] = bpp - 1

        if has_palette == 1:

            # Convert the palette into a string
            palette_string = bytearray()

            for (r1, g1, b1), (r2, g2, b2) in palette:
                word = (r1 << 8) | (g1 << 16) | (b1 << 24)
                palette_string = palette_string + self.number(4, word)
                word = (r2 << 8) | (g2 << 16) | (b2 << 24)
                palette_string = palette_string + self.number(4, word)

            # Return sprite, mask and palette strings
            return sprite, mask, palette_string
        else:
            return sprite, mask, bytearray()

    def cmyk2sprite(self, name):

        data = self.sprites[name]

        # Image data
        image = data['image']

        # Sprite and mask strings
        sprite = []

        # Write the image data to the sprite and mask
        ptr = 0

        for j in range(0, data['height']):

            for i in range(0, data['width']):
                # Read the red, green and blue components
                cyan = image[ptr]
                magenta = image[ptr + 1]
                yellow = image[ptr + 2]
                key = image[ptr + 3]

                # Store the components in the sprite word
                sprite_word = cyan | (magenta << 8) | (yellow << 16) | (key << 24)

                # store the word in the sprite string
                sprite.append(self.number(4, sprite_word))

        # Determine the actual number of words used per line of
        # the sprite.

        # The number of bits in the line.
        width_bits = data['width'] * 32
        # The excess number of bits (not filling a word completely).
        excess_bits = width_bits % 32
        # The number of excess words is therefore either one or zero.
        excess_words = excess_bits != 0
        # The number of words used.
        width = int(width_bits / 32) + excess_words

        self.sprites[name]['h_words'] = width
        self.sprites[name]['v_lines'] = data['height']

        self.sprites[name]['first bit'] = 0
        self.sprites[name]['last bit'] = 31

        return string.join(sprite, '')

    def rgb2cmyk(self, sprite, trans=None):

        image = sprite['image']

        if sprite['mode'] != 'RGB':
            raise spritefile_error('Image is not an RGB image.')

        cmyk = bytearray()

        if trans is None:

            # Default translation between RGB and CMYK.
            for i in range(0, len(image), 3):
                r, g, b = image[i], image[i + 1], image[i + 2]
                cyan = 255 - r
                magenta = 255 - g
                yellow = 255 - b
                key = int(((255 - r) + (255 - g) + (255 - b)) / 3.0)

                cmyk.append(cyan)
                cmyk.append(magenta)
                cmyk.append(yellow)
                cmyk.append(key)

        else:

            # Custom translation between RGB and CMYK.
            for i in range(0, len(image), 3):
                r, g, b = image[i], image[i + 1], image[i + 2]
                cyan, magenta, yellow, key = trans(r, g, b)

                cmyk.append(cyan)
                cmyk.append(magenta)
                cmyk.append(yellow)
                cmyk.append(key)

        new_sprite = \
            {
                'image': cmyk,
                'width': sprite['width'],
                'height': sprite['height'],
                'mode': 'CMYK',
                'bpp': 32, 'log2bpp': 5,
                'dpi x': sprite['dpi x'], 'dpi y': sprite['dpi y']
            }

        # Return the new sprite.
        return new_sprite

    def cmyk2rgb(self, sprite, trans=None):

        image = sprite['image']

        if sprite['mode'] != 'CMYK':
            raise spritefile_error('Image is not a CMYK image.')

        rgb = bytearray()

        if trans is None:

            # Default translation between CMYK and RGB.
            for i in range(0, len(image), 4):
                c, m, y = image[i], image[i + 1], image[i + 2]
                k = image[i + 3]
                red = 255 - c
                green = 255 - m
                blue = 255 - y

                rgb.append(red)
                rgb.append(green)
                rgb.append(blue)

        else:

            # Custom translation between CMYK and RGB.
            for i in range(0, len(image), 4):
                c, m, y = image[i], image[i + 1], image[i + 2]
                k = image[i + 3]

                red, green, blue = trans(c, m, y, k)

                rgb.append(red)
                rgb.append(green)
                rgb.append(blue)

        new_sprite = \
            {
                'image': rgb,
                'width': sprite['width'],
                'height': sprite['height'],
                'mode': 'RGB',
                'bpp': 32, 'log2bpp': 5,
                'dpi x': sprite['dpi x'], 'dpi y': sprite['dpi y']
            }

        # Return the new sprite.
        return new_sprite

    def write_details(self, file, name):

        # The details of the sprite
        data = self.sprites[name]

        if data['mode'] == 'RGB' or data['mode'] == 'RGBA':

            # Using the bits per pixel of the image, convert the
            # RGB or RGBA image to an appropriate pixel format.
            sprite, mask, palette = self.rgb2sprite(name)

        elif data['mode'] == 'CMYK':

            # Using the bits per pixel of the image, convert the
            # RGB or RGBA image to an appropriate pixel format.
            sprite = self.cmyk2sprite(name)
            mask = ''
            palette = ''

        # Determine the sprite header minus the offset to the next sprite.
        header = name[:12] + ((12 - len(name[:12])) * '\x00') + \
                 self.number(4, data['h_words'] - 1) + \
                 self.number(4, data['v_lines'] - 1) + \
                 self.number(4, data['first bit']) + \
                 self.number(4, data['last bit'])

        if mask != '':

            # Write offsets to sprite and mask.
            header = header + \
                     self.number(4, 4 + 12 + 16 + 8 + 4 + len(palette))
            header = header + \
                     self.number(4, 4 + 12 + 16 + 8 + 4 + len(palette) + len(sprite))
            # Note         ^   ^    ^    ^   ^
            #              |   |    |    |   bpp word
            #              |   |    |    sprite and mask offsets
            #              |   |    h_words, v_lines, first bit, last bit
            #              |   sprite name
            #              next sprite offset
        else:
            # Write the sprite offset twice.
            header = header + \
                     self.number(4, 4 + 12 + 16 + 8 + 4 + len(palette))
            header = header + \
                     self.number(4, 4 + 12 + 16 + 8 + 4 + len(palette))

        # Determine the screen mode from the bpp, xdpi and ydpi
        # The bits per pixel of the sprite is produced using a look-up table.
        if data['bpp'] == 1:
            log2bpp = 0
            mode = 1
        elif data['bpp'] == 2:
            log2bpp = 1
            mode = 2
        elif data['bpp'] == 4:
            log2bpp = 2
            mode = 3
        elif data['bpp'] == 8:
            log2bpp = 3
            mode = 4
        elif data['bpp'] == 16:
            log2bpp = 4
            mode = 5
        elif data['bpp'] == 32:
            log2bpp = 5
            mode = 6
        else:
            raise spritefile_error('Invalid number of bits per pixel in sprite.')

        mode_word = None

        if mode < 4:

            # Try to find an old style mode number for sprites with 16 colours or less.
            for mode_number, (lb, xs, ys) in list(self.mode_info.items()):

                if log2bpp == lb and data['dpi x'] == int(90 / xs) and data['dpi y'] == int(90 / ys):
                    mode_word = mode_number
                    break

        if mode_word is None:
            # Generate a new format mode description rather than an old style
            # mode number (bit 0 is set).
            mode_word = (mode << 27) | \
                        ((data['dpi x'] & 0x1fff) << 1) | \
                        ((data['dpi y'] & 0x1fff) << 14) | \
                        1

        header = header + self.number(4, mode_word)

        # Write the next sprite offset for this sprite:
        # this word + sprite header + palette + sprite + mask
        file.write(
            self.number(
                4, 4 + len(header) + len(palette) + len(sprite) + len(mask)))

        # Write the sprite header
        file.write(header)

        # Write the palette
        file.write(palette)

        # Write the image data
        file.write(sprite)

        # Write the mask
        file.write(mask)

        # Return the amount of data written to the file
        return 4 + len(header) + len(palette) + len(sprite) + len(mask)

    def write(self, file):
        """write(self, file)
        
        Write the sprites to a file specified by a file object.
        """

        # Count the sprites in the area
        number = len(list(self.sprites.keys()))
        file.write(self.number(4, number))

        # Put the sprites in the standard place
        offset = 16
        file.write(self.number(4, offset))

        # The free space offset points to after all the sprites
        # so we need to know how much space they take up.

        # Record the position of the free space pointer in the
        # file.
        free_ptr = file.tell()
        # Put a zero word in as a placeholder
        file.write(self.number(4, 0))

        # The offset will start after the number, first sprite offset
        # and free space offset.
        free = 16

        # Write the sprites to the file
        for name in list(self.sprites.keys()):
            free = free + self.write_details(file, name)

        # Fill in free space pointer
        file.seek(free_ptr, 0)
        file.write(self.number(4, free))
