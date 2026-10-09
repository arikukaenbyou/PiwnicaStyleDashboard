"""Optional infra alerts poller: config gating, summarising, keeping the last good data."""
import unittest

from piwnica_dashboard.collect.infra import InfraPoller, summarize

PAYLOAD = {'counts': {'critical': 1, 'warning': 2, 'info': 9},
           'alerts': [{'level': 'info', 'title': 'a', 'reason': 'newer version'},
                      {'level': 'warning', 'title': 'b', 'reason': 'updates waiting'},
                      {'level': 'critical', 'title': 'c', 'reason': 'down'},
                      {'level': 'bogus', 'title': 'd'}]}


class TestInfra(unittest.TestCase):
    def test_off_without_url_or_token(self):
        self.assertIsNone(InfraPoller.from_config({}))
        self.assertIsNone(InfraPoller.from_config({'infra': {'url': 'https://x/api', 'token': ''}}))
        self.assertIsNotNone(InfraPoller.from_config({'infra': {'url': 'https://x/api', 'token': 't'}}))

    def test_summarize_orders_by_urgency_and_drops_info(self):
        s = summarize(PAYLOAD)
        self.assertEqual(s['counts'], {'critical': 1, 'warning': 2, 'info': 9})
        self.assertEqual([a['title'] for a in s['top']], ['c', 'b'])

    def test_error_keeps_last_good_counts(self):
        calls = iter([PAYLOAD, OSError('down')])

        def fetcher(url, token):
            v = next(calls)
            if isinstance(v, Exception):
                raise v
            return v

        p = InfraPoller('https://x/api', 't', fetcher=fetcher)
        self.assertIsNone(p.poll_once()['error'])
        after = p.poll_once()
        self.assertEqual(after['counts']['critical'], 1)
        self.assertEqual(after['error'], 'OSError')

    def test_interval_has_a_floor(self):
        self.assertEqual(InfraPoller('u', 't', interval=5).interval, 60)


if __name__ == '__main__':
    unittest.main()
