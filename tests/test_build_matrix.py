import unittest

import tools.validate_build_matrix as matrix


class BuildMatrixTests(unittest.TestCase):
    def test_current_device_and_firmware_matrix_is_complete(self):
        matrix.validate()


if __name__ == "__main__":
    unittest.main()
