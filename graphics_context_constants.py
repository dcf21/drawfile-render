# -*- coding: utf-8 -*-
# graphics_context_constants.py
#
# The Python script in this file is part of drawfile-render: a tool for
# rendering Acorn !Draw files to PNG and SVG.
#
# Copyright (C) 2010-2023 Dominic Ford <https://dcford.org.uk/>
#
# This code is free software; you can redistribute it and/or modify it under
# the terms of the GNU General Public License as published by the Free Software
# Foundation; either version 2 of the License, or (at your option) any later
# version.
#
# You should have received a copy of the GNU General Public License along with
# this file; if not, write to the Free Software Foundation, Inc., 51 Franklin
# Street, Fifth Floor, Boston, MA  02110-1301, USA

# ----------------------------------------------------------------------------

"""
The file contains global settings for graphics_context output.
"""

from math import pi

# Units
dots_per_inch = 200

unit_m = 1.
unit_cm = 1. / 100
unit_mm = 1. / 1000

# Angle conversion
unit_deg = float(pi / 180)
unit_rev = 2. * pi

# Font size
font_size_base = 3.2 * unit_mm
line_width_base = 0.2 * unit_mm
