"""Hermetic one-shot Arthexis importer tests; no live database access."""
import importlib.util
import json
import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

spec = importlib.util.spec_from_file_location("legacy", Path(__file__).resolve().parents[1] / "scripts" / "import_legacy_arthexis.py")
legacy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(legacy)

SCHEMA = """
CREATE TABLE sources(source_id TEXT PRIMARY KEY,cursor INTEGER NOT NULL DEFAULT 0,last_ingested_at TEXT);
CREATE TABLE current_transactions(source_id TEXT,charger_id TEXT,transaction_id INTEGER,
 page_after INTEGER,page_next INTEGER,fingerprint TEXT,payload TEXT,imported_at TEXT,
 PRIMARY KEY(source_id,charger_id,transaction_id));
CREATE TABLE transaction_observations(source_id TEXT,charger_id TEXT,transaction_id INTEGER,
 page_after INTEGER,page_next INTEGER,fingerprint TEXT,payload TEXT,imported_at TEXT,
 PRIMARY KEY(source_id,charger_id,transaction_id,page_after,page_next,fingerprint));
CREATE TABLE replication_changes(sequence INTEGER PRIMARY KEY AUTOINCREMENT,source_id TEXT,
 kind TEXT,record_key TEXT,payload TEXT);
CREATE TRIGGER replicate_transaction_insert AFTER INSERT ON current_transactions BEGIN
 INSERT INTO replication_changes(source_id,kind,record_key,payload) VALUES
 (NEW.source_id,'transaction', NEW.charger_id||':'||NEW.transaction_id,NEW.payload);
END;
"""
OLD = """
CREATE TABLE ocpp_charger(id INTEGER PRIMARY KEY,charger_id TEXT,connector_id INTEGER);
CREATE TABLE ocpp_transaction(id INTEGER PRIMARY KEY,charger_id INTEGER,connector_id INTEGER,
 ocpp_transaction_id TEXT,meter_start INTEGER,meter_stop INTEGER,start_time TEXT,stop_time TEXT,
 stop_reason TEXT,is_deleted INTEGER,is_seed_data INTEGER);
"""

class TestLegacyBackfill(unittest.TestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        folder=Path(self.tmp.name)
        self.old=folder/'old.sqlite3';self.target=folder/'reports.sqlite3';self.bak=folder/'backup.sqlite3'
        with sqlite3.connect(self.old) as db:
            db.executescript(OLD)
            db.execute("INSERT INTO ocpp_charger VALUES (2,'SERIAL-123',1)")
            db.executemany("INSERT INTO ocpp_transaction VALUES (?,?,?,?,?,?,?,?,?,?,?)",[
                (1,2,1,'10',1000,4500,'2026-05-05 00:01:00','2026-05-05 01:01:00','Local',0,0),
                (2,2,1,'10',None,None,'2026-06-05 00:01:00',None,'',0,0),
                (3,2,1,'10',1000,4500,'2026-05-05 00:01:00','2026-05-05 01:01:00','Local',0,0),
            ])
        with sqlite3.connect(self.target) as db:
            db.executescript(SCHEMA)
            db.execute("INSERT INTO sources VALUES ('gway-002',400,'2026-11-10T00:00:00Z')")
            now={'id':8,'charger_id':'SERIAL-123','connector_id':1,'started_at':'2026-10-10T00:00:00.000Z','energy_wh':2000}
            db.execute("INSERT INTO current_transactions VALUES (?,?,?,?,?,?,?,?)",
                       ('gway-002','SERIAL-123',8,20,30,'fingerprint',json.dumps(now),'2026-11-10T00:00:00Z'))
    def query(self,sql,params=()):
        with sqlite3.connect(self.target) as db:return db.execute(sql,params).fetchall()
    def go(self,**kwargs):
        return legacy.import_once(self.old,self.target,'gway-002',apply=kwargs.get('apply',False),
                                  backup=kwargs.get('backup'), archive_tag=kwargs.get('archive_tag', 'primary'))
    def test_dry_run_does_not_modify_target(self):
        before=self.target.read_bytes()
        plan=self.go()
        self.assertEqual(plan['to_insert'],2)
        self.assertEqual(plan['exact_duplicates_skipped'],1)
        self.assertEqual(plan['incomplete'],1)
        self.assertEqual(before,self.target.read_bytes())
        self.assertFalse(self.bak.exists())
    def test_one_shot_replay_and_existing_source_cursor(self):
        out=self.go(apply=True,backup=self.bak)
        self.assertEqual(out['inserted'],2)
        self.assertTrue(self.bak.exists())
        self.assertEqual(self.query('select cursor from sources'),[(400,)])
        rows=self.query('select transaction_id,payload from current_transactions order by transaction_id')
        self.assertEqual(len(rows),3)
        self.assertEqual([r[0] for r in rows],[8,1000000001,1000000002])
        completed=json.loads(rows[1][1]);pending=json.loads(rows[2][1])
        self.assertEqual(completed['energy_wh'],3500)
        self.assertEqual(completed['started_at'],'2026-05-05T00:01:00.000Z')
        self.assertNotIn('rfid',completed)
        self.assertIsNone(pending['energy_wh'])
        self.assertEqual(pending['state'],'incomplete')
        self.assertFalse(pending['active'])
        self.assertEqual(self.query("select count(*) from replication_changes where kind='transaction'"),[(3,)])
        self.assertEqual(self.go(apply=True)['to_insert'],0)
        self.assertEqual(self.query('select count(*) from current_transactions'),[(3,)])
        self.assertEqual(self.query('select count(*) from replication_changes'),[(3,)])
    def test_fails_closed_if_a_legacy_row_changes_after_import(self):
        self.go(apply=True,backup=self.bak)
        with sqlite3.connect(self.old) as db:db.execute('UPDATE ocpp_transaction SET meter_stop=5500 WHERE id=1')
        with self.assertRaisesRegex(ValueError,'Historical identity conflict'):self.go()
    def test_fails_closed_on_source_mismatch(self):
        with self.assertRaisesRegex(ValueError,'source'):legacy.import_once(self.old,self.target,'gway-005',apply=False,backup=None)
    def test_fails_closed_on_existing_synthetic_key(self):
        with sqlite3.connect(self.target) as db:
            db.execute("INSERT INTO current_transactions VALUES (?,?,?,?,?,?,?,?)",
                       ('gway-002','SERIAL-123',1000000001,0,0,'x','{}','x'))
        with self.assertRaisesRegex(ValueError,'Historical identity conflict'):self.go()
    def test_no_backup_no_write(self):
        with self.assertRaisesRegex(ValueError,'--backup'):self.go(apply=True)
        self.assertEqual(self.query('select count(*) from current_transactions'),[(1,)])
    def test_rejects_overlap_with_current_csms(self):
        with sqlite3.connect(self.old) as db:db.execute("UPDATE ocpp_transaction SET start_time='2026-10-10 00:00:00', stop_time='2026-10-10 01:00:00' WHERE id=1")
        with self.assertRaisesRegex(ValueError,'overlaps'):self.go()
    def test_fail_closed_incomplete_old_schema(self):
        with sqlite3.connect(self.old) as db:db.execute('ALTER TABLE ocpp_transaction RENAME TO old_transactions')
        with self.assertRaisesRegex(ValueError,'Unsupported'):self.go()


class TestArchivedSchemaCompatibility(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.old = root / "failover-2025.sqlite3"
        self.target = root / "warehouse.sqlite3"
        with sqlite3.connect(self.old) as db:
            # Observed pre-2026 Arthexis transaction columns omit the legacy OCPP ID.
            db.executescript("""
                CREATE TABLE ocpp_charger(id INTEGER PRIMARY KEY, charger_id TEXT, connector_id INTEGER);
                CREATE TABLE ocpp_transaction(id INTEGER PRIMARY KEY,charger_id INTEGER,connector_id INTEGER,
                    meter_start INTEGER,meter_stop INTEGER,start_time TEXT,stop_time TEXT,
                    is_deleted INTEGER,is_seed_data INTEGER);
            """)
            db.execute("INSERT INTO ocpp_charger VALUES (3,'SERIAL-123',2)")
            db.execute("INSERT INTO ocpp_transaction VALUES (1,3,2,1000,3000,?, ?,0,0)",
                       ('2025-10-02 00:00:00.000000', '2025-10-02 01:00:00.000000'))
        with sqlite3.connect(self.target) as db:
            db.executescript(SCHEMA)
            db.execute("INSERT INTO sources VALUES ('gway-005',400,'2026-11-10T00:00:00Z')")
            recent = {"id": 2, "charger_id": "SERIAL-123", "connector_id": 2,
                      "started_at": "2026-10-10T00:00:00.000Z"}
            db.execute("INSERT INTO current_transactions VALUES (?,?,?,?,?,?,?,?)",
                       ('gway-005','SERIAL-123',2,1,4,'x',json.dumps(recent),'2026-11-10T00:00:00Z'))

    def test_2025_archive_uses_independent_namespace_without_invented_samples(self):
        result = legacy.import_once(self.old, self.target, 'gway-005', archive_tag='failover-2025',
                                    apply=True, backup=self.target.with_name('backup.sqlite3'))
        self.assertEqual(result['inserted'], 1)
        with sqlite3.connect(self.target) as db:
            row = db.execute('SELECT transaction_id,payload FROM current_transactions WHERE transaction_id > 1000000000').fetchone()
            self.assertEqual(row[0], 2000000001)
            payload = json.loads(row[1])
            self.assertEqual(payload['historical_provenance']['archive_tag'], 'failover-2025')
            self.assertEqual(payload['energy_wh'], 2000)
            self.assertEqual(payload['started_at'], '2025-10-02T00:00:00.000Z')
            self.assertEqual(db.execute("SELECT count(*) FROM replication_changes WHERE kind='meter_sample'").fetchone()[0],0)
            self.assertEqual(db.execute('SELECT cursor FROM sources').fetchone()[0],400)

    def test_wrong_archive_tag_is_refused(self):
        with self.assertRaisesRegex(ValueError, 'archive tag'):
            legacy.import_once(self.old,self.target,'gway-005',archive_tag='primary',apply=False,backup=None)

    def test_incomplete_session_with_meter_stop_is_not_a_success(self):
        with sqlite3.connect(self.old) as db:
            db.execute('UPDATE ocpp_transaction SET stop_time=NULL')
        result = legacy.import_once(self.old,self.target,'gway-005',archive_tag='failover-2025',
                                    apply=True,backup=self.target.with_name('backup.sqlite3'))
        self.assertEqual(result['incomplete'],1)
        with sqlite3.connect(self.target) as db:
            payload = json.loads(db.execute('SELECT payload FROM current_transactions WHERE transaction_id=2000000001').fetchone()[0])
            self.assertIsNone(payload['energy_wh'])
            self.assertEqual(payload['state'],'incomplete')
            self.assertTrue(payload['quality']['missing_meter'])

    def test_no_write_on_dry_run_even_for_old_schema(self):
        before = self.target.read_bytes()
        p = legacy.import_once(self.old,self.target,'gway-005',archive_tag='failover-2025',
                               apply=False,backup=None)
        self.assertEqual(p['to_insert'],1)
        self.assertEqual(self.target.read_bytes(),before)

if __name__ == '__main__':
    unittest.main()
