"""核对原始业务资料之间的引用与记录形式。"""

import unittest

from inspect_samples import read_samples


class SampleTests(unittest.TestCase):
    def setUp(self):
        self.data = read_samples()

    def test_copies_and_households_have_anchors(self):
        copies = {row['id'] for row in self.data['copies']}
        households = {row['id']: row for row in self.data['households']}
        for row in self.data['logs']:
            self.assertIn(row['copy'], copies)
            if 'household' in row:
                self.assertIn(row['household'], households)
        for row in self.data['commitments']:
            self.assertIn(row['copy'], copies)

    def test_records_include_neighbor_fulfillment_and_late_receipt(self):
        households = {row['id']: row['home'] for row in self.data['households']}
        self.assertTrue(any(row['kind'] == '馆际代取' and row['branch'] != households[row['household']] for row in self.data['logs']))
        self.assertTrue(any(row['receipt_week'] > row['week'] for row in self.data['logs']))
