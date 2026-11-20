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

    def test_reading_cycles_and_copy_obligations(self):
        works = {row['work'] for row in self.data['copies']}
        copies = {row['id']: row for row in self.data['copies']}
        self.assertTrue(any(len(item['accepted']) > 1 for request in self.data['requests'] for item in request['works']))
        for request in self.data['requests']:
            self.assertLess(request['opens_week'], request['deadline_week'])
            for item in request['works']:
                self.assertLessEqual(set(item['accepted']), works)
        commitments = {row['copy'] for row in self.data['commitments']}
        self.assertTrue(any(move['copy'] in commitments for option in self.data['options'] for move in option['moves']))
        self.assertTrue(any(row['kind']=='续期' and row['start_week'] < row['received_week'] and row['copy'] in copies for row in self.data['supply']))
        by_request = {row['id']:row for row in self.data['requests']}
        for row in self.data['fulfillments']:
            request = by_request[row['request']]
            self.assertIn(row['pickup'],request['pickup'])
            self.assertTrue(request['opens_week'] <= row['day'] < request['deadline_week'])
