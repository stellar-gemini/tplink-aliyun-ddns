import copy
import importlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
app = importlib.import_module('tplink_ddns')

IP = '2001:db8:1::10'
OTHER = '2001:db8:2::20'
MAC = '02:00:00:00:00:01'
B = app.Binding(app.normalize_mac(MAC), 'example.com', 'nas')


def response(data, status=200):
    return Mock(status_code=status, json=Mock(return_value=data))


def row(value=OTHER, record_id='1', line='default', **extra):
    return dict(DomainName='example.com', RR='nas', Type='AAAA', Line=line,
                RecordId=record_id, Status='Enable', Value=value, **extra)


class MemoryDNS(app.AliDNS):
    def __init__(self, records=None):
        self.rows = records or []
        self.ttl = 600
        self.calls = []
        self.fail_update = False

    def call(self, action, **params):
        self.calls.append((action, params))
        if action == 'DescribeSubDomainRecords':
            page = params['PageNumber']
            return {'TotalCount': len(self.rows), 'DomainRecords': {'Record': copy.deepcopy(self.rows[(page-1)*100:page*100])}}
        if self.fail_update:
            raise app.DDNSError('simulated failure')
        if action == 'AddDomainRecord':
            self.rows.append(row(params['Value'], str(len(self.rows)+1), params['Line']))
            return {'RecordId': self.rows[-1]['RecordId']}
        assert action == 'UpdateDomainRecord'
        for record in self.rows:
            if record['RecordId'] == params['RecordId']:
                record['Value'] = params['Value']
                return {'RecordId': record['RecordId']}
        raise AssertionError('unexpected record ID')


class DNSTests(unittest.TestCase):
    def test_create_missing(self):
        dns = MemoryDNS()
        self.assertEqual(dns.sync(B, IP), 'created')
        self.assertEqual([a for a, _ in dns.calls], ['DescribeSubDomainRecords', 'AddDomainRecord'])
        self.assertEqual(dns.calls[-1][1], dict(DomainName='example.com', RR='nas', Type='AAAA', Value=IP, TTL=600, Line='default'))

    def test_update_by_record_id(self):
        dns = MemoryDNS([row()])
        self.assertEqual(dns.sync(B, IP), 'updated')
        self.assertEqual(dns.rows[0]['Value'], IP)
        self.assertEqual(dns.calls[-1][1]['RecordId'], '1')

    def test_same_address_different_notation_no_write(self):
        dns = MemoryDNS([row('2001:0db8:0001:0000:0000:0000:0000:0010')])
        self.assertEqual(dns.sync(B, IP), 'unchanged')
        self.assertEqual(len(dns.calls), 1)

    def test_dry_run_does_not_write(self):
        for rows in ([], [row()]):
            dns = MemoryDNS(copy.deepcopy(rows))
            self.assertEqual(dns.sync(B, IP, True), 'dry-run')
            self.assertEqual(len(dns.calls), 1)
            self.assertEqual(dns.rows, rows)

    def test_ambiguous_record_requires_id(self):
        dns = MemoryDNS([row(record_id='1'), row(record_id='2')])
        with self.assertRaises(app.DDNSError):
            dns.sync(B, IP)
        self.assertEqual(len(dns.calls), 1)
        selected = app.Binding(B.mac, B.domain, B.rr, record_id='2')
        dns.sync(selected, IP)
        self.assertEqual(dns.rows[0]['Value'], OTHER)
        self.assertEqual(dns.rows[1]['Value'], IP)

    def test_missing_explicit_id_does_not_create(self):
        dns = MemoryDNS()
        with self.assertRaises(app.DDNSError):
            dns.sync(app.Binding(B.mac, B.domain, B.rr, record_id='99'), IP)
        self.assertEqual(len(dns.calls), 1)

    def test_other_lines_and_rr_are_not_modified(self):
        dns = MemoryDNS([row(line='telecom')])
        dns.sync(B, IP)
        self.assertEqual(dns.rows[0]['Value'], OTHER)
        self.assertEqual(dns.calls[-1][0], 'AddDomainRecord')

    def test_disabled_record_is_not_silent_success(self):
        record = row(IP)
        record['Status'] = 'Disable'
        with self.assertRaises(app.DDNSError):
            MemoryDNS([record]).sync(B, IP)

    def test_pagination_finds_explicit_id(self):
        dns = MemoryDNS([row(record_id=str(i)) for i in range(101)])
        dns.sync(app.Binding(B.mac, B.domain, B.rr, record_id='100'), IP)
        self.assertEqual([p['PageNumber'] for a, p in dns.calls if a == 'DescribeSubDomainRecords'], [1, 2])
        self.assertEqual(dns.rows[-1]['Value'], IP)

    def test_mutation_requires_record_id_response(self):
        dns = MemoryDNS()
        dns.call = Mock(side_effect=[{'TotalCount': 0, 'DomainRecords': {'Record': []}}, {}])
        with self.assertRaises(app.DDNSError):
            dns.sync(B, IP)


class RunnerTests(unittest.TestCase):
    def setUp(self):
        # Documentation addresses are intentionally rejected by production filtering.
        # Runner tests inject only these two fixtures to exercise synchronization.
        original_filter = app.global_ipv6
        def fixture_address(value):
            if value in (IP, OTHER):
                return value
            return original_filter(value)
        classifier = patch.object(app, 'global_ipv6', side_effect=fixture_address)
        classifier.start()
        self.addCleanup(classifier.stop)
        self.router = Mock()
        self.router.get_hosts.return_value = [{'mac': MAC, 'ipv6': IP}]
        self.dns = MemoryDNS([row()])
        self.runner = app.Runner(self.router, self.dns, [B], 1800)

    def test_cache_change_and_periodic_reconcile(self):
        with patch.object(app.time, 'monotonic', return_value=1):
            self.assertTrue(self.runner.cycle())
        self.dns.calls.clear()
        with patch.object(app.time, 'monotonic', return_value=100):
            self.assertTrue(self.runner.cycle())
        self.assertEqual(self.dns.calls, [])
        # Service-side edit is repaired when reconciliation is due.
        self.dns.rows[0]['Value'] = OTHER
        with patch.object(app.time, 'monotonic', return_value=1801):
            self.assertTrue(self.runner.cycle())
        self.assertEqual(self.dns.rows[0]['Value'], IP)
        self.router.get_hosts.return_value[0]['ipv6'] = OTHER
        with patch.object(app.time, 'monotonic', return_value=1802):
            self.assertTrue(self.runner.cycle())
        self.assertEqual(self.dns.rows[0]['Value'], OTHER)

    def test_failure_not_cached_retry_next_cycle(self):
        self.dns.fail_update = True
        self.assertFalse(self.runner.cycle())
        self.assertEqual(self.runner.cache, {})
        self.dns.fail_update = False
        self.assertTrue(self.runner.cycle())
        self.assertEqual(self.dns.rows[0]['Value'], IP)

    def test_offline_and_invalid_ipv6_do_not_write(self):
        for address in (None, '', '::', '::1', 'fe80::1', 'fd00::1', 'ff02::1',
                        '2001:db8::1', '::ffff:192.0.2.10', '192.0.2.10', 'bad', IP + '/64', IP + '%eth0'):
            self.router.get_hosts.return_value = [{'mac': MAC, 'ipv6': address}]
            self.assertFalse(self.runner.cycle())
        self.router.get_hosts.return_value = []
        self.assertFalse(self.runner.cycle())
        self.assertEqual(self.dns.calls, [])

    def test_conflicting_duplicate_mac_is_skipped(self):
        self.router.get_hosts.return_value.append({'mac': MAC.upper().replace('-', ':'), 'ipv6': OTHER})
        self.assertFalse(self.runner.cycle())
        self.assertEqual(self.dns.calls, [])

    def test_failure_does_not_block_other_bindings(self):
        other_binding = app.Binding(B.mac, B.domain, 'other')
        self.runner.bindings.append(other_binding)
        self.dns.sync = Mock(side_effect=[app.DDNSError('failure'), 'created'])
        self.assertFalse(self.runner.cycle())
        self.assertNotIn(B, self.runner.cache)
        self.assertIn(other_binding, self.runner.cache)

    def test_cached_cycles_still_emit_summary(self):
        self.assertTrue(self.runner.cycle())
        self.dns.calls.clear()
        with self.assertLogs(app.LOG, level='INFO') as captured:
            self.assertTrue(self.runner.cycle())
        self.assertEqual(self.dns.calls, [])
        summary = '\n'.join(captured.output)
        self.assertIn('本轮完成', summary)
        self.assertIn('缓存跳过 1', summary)


class LoggingTests(unittest.TestCase):
    def test_application_logs_are_flushed_to_stdout(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        root = app.logging.getLogger()
        old_handlers, old_level = root.handlers[:], root.level
        old_disabled = root.manager.disable
        urllib_logger = app.logging.getLogger('urllib3')
        old_urllib_level = urllib_logger.level
        try:
            root.handlers = []
            app.logging.disable(app.logging.NOTSET)
            with patch.object(sys, 'stdout', stdout), patch.object(sys, 'stderr', stderr):
                app.configure_logging()
                app.LOG.info('startup-visible')
                app.LOG.error('error-visible')
                self.assertIn('startup-visible', stdout.getvalue())
                self.assertIn('error-visible', stdout.getvalue())
                self.assertEqual(stderr.getvalue(), '')
                self.assertEqual(urllib_logger.level, app.logging.WARNING)
        finally:
            for handler in root.handlers:
                handler.close()
            root.handlers = old_handlers
            root.setLevel(old_level)
            urllib_logger.setLevel(old_urllib_level)
            app.logging.disable(old_disabled)


class AddressFilterTests(unittest.TestCase):
    def test_documentation_addresses_are_not_public(self):
        for address in (IP, OTHER, '2001:db8::1'):
            self.assertIsNone(app.global_ipv6(address))


class ProtocolTests(unittest.TestCase):
    def test_signature_official_aliyun_golden_vector(self):
        # https://help.aliyun.com/zh/cmn/developer-reference/signature-mechanism
        params = {'Action': 'DescribeRegions', 'Format': 'XML', 'Version': '2014-05-26',
                  'AccessKeyId': 'testid', 'SignatureMethod': 'HMAC-SHA1',
                  'Timestamp': '2016-02-23T12:46:24Z', 'SignatureVersion': '1.0',
                  'SignatureNonce': '3ee8c1b8-83d3-44af-a94f-4e0ad82fd6cf'}
        self.assertEqual(app.aliyun_signature(params, 'testsecret'), 'OLeaidS1JvxuMvnyHOwuJ+uX5qY=')

    @patch.dict(os.environ, {'TPLINK_PASSWORD': 'password'})
    def test_session_reauth_exactly_once(self):
        client = app.RouterClient({'url': 'https://router'}, 10)
        payload = {'error_code': 0, 'hosts_info': {'online_host': []}}
        client.session.post = Mock(side_effect=[response({'error_code': 0, 'stok': 'old'}),
            response({'error_code': -40401}), response({'error_code': 0, 'stok': 'new'}), response(payload)])
        self.assertEqual(client.get_hosts(), [])
        calls = client.session.post.call_args_list
        self.assertEqual([c.args[0] for c in calls], ['https://router/', 'https://router/stok=old/ds',
                                                   'https://router/', 'https://router/stok=new/ds'])
        self.assertEqual(calls[-1].kwargs['json'], {'hosts_info': {'table': 'online_host'}, 'method': 'get'})
        client.session.post.side_effect = [response({}, 403), response({'error_code': 0, 'stok': 'again'}), response({}, 403)]
        with self.assertRaises(app.DDNSError):
            client.get_hosts()
        self.assertIsNone(client.stok)
        client.close()

    @patch.dict(os.environ, {'TPLINK_PASSWORD': 'password'})
    def test_http_500_does_not_trigger_login(self):
        client = app.RouterClient({'url': 'https://router'}, 10)
        client.stok = 'old'
        client.session.post = Mock(return_value=response({}, 500))
        with self.assertRaises(app.DDNSError):
            client.get_hosts()
        self.assertEqual(client.session.post.call_count, 1)
        self.assertEqual(client.stok, 'old')
        client.close()

    @patch.dict(os.environ, {'TPLINK_PASSWORD': 'wrong'})
    def test_bad_login_backoff_avoids_password_storm(self):
        client = app.RouterClient({'url': 'https://router'}, 10)
        client.session.post = Mock(return_value=response({'error_code': -40201}))
        for _ in range(3):
            with self.assertRaises(app.DDNSError):
                client.get_hosts()
        self.assertEqual(client.session.post.call_count, 1)
        client.close()

    @patch.dict(os.environ, {'ALIBABA_CLOUD_ACCESS_KEY_ID': 'fake-id', 'ALIBABA_CLOUD_ACCESS_KEY_SECRET': 'fake-secret'})
    def test_api_errors_and_timeouts_hide_signed_url(self):
        dns = app.AliDNS({'ttl': 600}, 10)
        dns.session.get = Mock(return_value=response({'Code': 'Forbidden.RAM', 'Message': 'secret-details'}, 403))
        with self.assertRaises(app.DDNSError) as cm:
            dns.call('DescribeSubDomainRecords', SubDomain='nas.example.com')
        self.assertIn('Forbidden.RAM', str(cm.exception))
        self.assertNotIn('secret', str(cm.exception))
        args = dns.session.get.call_args
        self.assertEqual(args.args[0], 'https://alidns.aliyuncs.com/')
        self.assertIn('Signature', args.kwargs['params'])
        self.assertTrue(dns.session.verify)
        dns.session.get.side_effect = app.requests.Timeout('https://endpoint/?Signature=SECRET')
        with self.assertRaises(app.DDNSError) as cm:
            dns.call('UpdateDomainRecord')
        self.assertNotIn('SECRET', str(cm.exception))
        dns.close()

    def test_example_config_and_duplicate_validation(self):
        cfg = app.load_config(ROOT / 'ddns_config.example.json')
        self.assertEqual(len(cfg['bindings']), 3)
        self.assertEqual(cfg['bindings'][1].fqdn, 'storage.home.example.com')
        raw = json.loads((ROOT / 'ddns_config.example.json').read_text())
        raw['maclist'].append(copy.deepcopy(raw['maclist'][0]))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'config.json'
            path.write_text(json.dumps(raw))
            with self.assertRaises(ValueError):
                app.load_config(path)


if __name__ == '__main__':
    app.logging.disable(app.logging.CRITICAL)
    unittest.main(verbosity=2)
