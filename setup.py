# This file is part of MSMAccelerator.
#
# Copyright 2011 Stanford University
#
# MSMAccelerator is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 59 Temple Place, Suite 330, Boston, MA  02111-1307  USA

from setuptools import setup
import glob

# install also requires a pypi thing called 'MySQL-python' which provides
# the package mysqldb

setup(name='msmaccelerator',
      install_requires=['pyyaml', 'sqlalchemy'],
      version = '0.1',
      packages = ['msmaccelerator', 'msmaccelerator.scripts', 'msmaccelerator.adaptive'],
      package_dir = {'msmaccelerator': 'lib',
                     'msmaccelerator.scripts': 'scripts',
                     'msmaccelerator.adaptive': 'lib/adaptive'},
      scripts = glob.glob('scripts/*'),
)
