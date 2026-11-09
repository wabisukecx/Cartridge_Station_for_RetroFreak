"""Public product names shared by frontends and distribution builders."""
PRODUCT_NAME = 'Cartridge Station for RetroFreak'
WINDOWS_EDITION = 'Windows Edition'
MISTER_EDITION = 'MiSTer FPGA Edition'
INSTALL_DIRECTORY = 'Cartridge_Station_for_RetroFreak'
WINDOWS_LAUNCHER = INSTALL_DIRECTORY + '_Windows.cmd'
MISTER_LAUNCHER = INSTALL_DIRECTORY + '.sh'
WINDOWS_ARCHIVE = INSTALL_DIRECTORY + '_Windows'
MISTER_ARCHIVE = INSTALL_DIRECTORY + '_MiSTer'
# Share the lock with the compatibility-named app to prevent concurrent reader use.
MISTER_LOCK_PATH = '/tmp/mister-cart-station.lock'
