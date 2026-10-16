import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import uuid

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/draft_history.py'
spec = importlib.util.spec_from_file_location('draft_history', SCRIPT)
history = importlib.util.module_from_spec(spec); spec.loader.exec_module(history)
BODY = '出门总在包底找钥匙，手里还提着早餐，只想快点找到。这个蓝色棉布钥匙包可以装钥匙，挂在包内就能直接摸到。对我来说，收纳的位置固定下来，比在门口翻半天更省事。'
OTHER = '桌面上散着几把备用钥匙，收拾抽屉时才发现它们各有用处。棉布小袋刚好能把这些零碎放在一起，蓝色也容易在抽屉里辨认。搬家打包时把小件集中收好，之后取用就不用逐个盒子找了。'
TOPICS = '#钥匙收纳  #蓝色\n#钥匙收纳'


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / 'history.sqlite3'
        history.initialize(self.db)

    def job(self, body=BODY, angle='门口双手拿早餐找钥匙', **changes):
        result = dict(request_id=str(uuid.uuid4()), account='账号甲', product='棉布钥匙包', mode=5,
                      source_topics=TOPICS, drafts=[dict(title='', body=body, topics=TOPICS, angle=angle,
                          variation=dict(scene='drawer' if body==OTHER else 'breakfast',
                                         focus='sorting' if body==OTHER else 'finding',structure='problem_choice'))])
        result.update(changes); return result

    def action(self, job, action):
        c = history.connect(self.db)
        try: return history.process(c, job, action)
        finally: c.close()

    def review(self, job):
        result, code = self.action(job, 'check'); self.assertEqual(code, 0)
        job['semantic_review'] = dict(status='passed', payload_hash=result['payload_hash'],
            compared_ids=result['required_review_ids'], batch_reviewed=True,
            notes='人工构造测试用例：正文事实与话题保留，检查本批叙事切口不同。')
        return job

    def cli(self, job, action):
        path = Path(self.temp.name) / (str(uuid.uuid4()) + '.json')
        path.write_text(json.dumps(job, ensure_ascii=False), encoding='utf-8')
        result = subprocess.run([sys.executable, str(SCRIPT), '--db', str(self.db), action,
                                 '--input', str(path)], capture_output=True, text=True)
        return result.returncode, json.loads(result.stdout)

    def test_persists_across_independent_processes(self):
        first = self.review(self.job()); code, result = self.cli(first, 'commit')
        self.assertEqual(code, 0); self.assertEqual(result['status'], 'recorded')
        code, result = self.cli(self.job(), 'check')
        self.assertEqual(code, 2); self.assertEqual(result['matches'][0]['reason'], 'exact_body')

    def test_punctuation_emoji_whitespace_and_title_cannot_hide_duplicate(self):
        self.action(self.job(), 'import')
        job = self.job(BODY.replace('，', ' ✨！！\n').replace('。', '。🙂'), angle='另写的角度')
        job['drafts'][0]['title'] = '换一个新标题'
        result, code = self.action(job, 'check')
        self.assertEqual(code, 2); self.assertEqual(result['matches'][0]['reason'], 'exact_body')

    def test_minor_word_change_is_rejected(self):
        self.action(self.job(), 'import')
        result, code = self.action(self.job(BODY.replace('早餐','饭盒').replace('更省事','更方便'), angle='另一个角度'), 'check')
        self.assertEqual(code, 2); self.assertEqual(result['matches'][0]['reason'], 'near_body')

    def test_batch_duplicate_rejects_entire_transaction(self):
        job = self.job(); job['drafts'].append(copy.deepcopy(job['drafts'][0]))
        result, code = self.action(job, 'commit'); self.assertEqual(code, 2)
        c = history.connect(self.db); self.assertEqual(len(history.rows(c,'账号甲','棉布钥匙包')),0); c.close()

    def test_different_draft_preserves_topics_and_requires_review(self):
        self.action(self.job(), 'import')
        job = self.job(OTHER, '搬家时把抽屉的备用钥匙集中收纳')
        self.assertEqual(self.action(job, 'commit')[1], 4)
        result, code = self.action(self.review(job), 'commit')
        self.assertEqual(code,0); self.assertEqual(result['drafts'][0]['topics'],TOPICS)
        self.assertEqual(result['drafts'][0]['body'],OTHER)

    def test_changed_body_invalidates_semantic_review(self):
        job = self.review(self.job()); job['drafts'][0]['body'] += '这句是检查后又加入的。'
        self.assertEqual(self.action(job,'commit')[1],4)

    def test_new_history_requires_new_review(self):
        pending = self.review(self.job(OTHER, '搬家收纳'))
        self.action(self.job(), 'import')
        self.assertEqual(self.action(pending, 'commit')[1],4)

    def test_accounts_and_products_are_isolated_but_case_is_stable(self):
        self.action(self.job(account='Account A'), 'import')
        self.assertEqual(self.action(self.job(account=' account a '), 'check')[1],2)
        self.assertEqual(self.action(self.job(account='Account B'), 'check')[1],0)
        self.assertEqual(self.action(self.job(account='Account A',product='其他产品'), 'check')[1],0)

    def test_missing_corrupt_or_replaced_history_never_resets(self):
        self.db.unlink()
        with self.assertRaises(ValueError):history.initialize(self.db)
        with self.assertRaises(ValueError):history.connect(self.db)
        self.db.write_bytes(b'not a sqlite database')
        with self.assertRaises(Exception):history.connect(self.db)
        self.assertEqual(self.db.read_bytes(),b'not a sqlite database')

    def test_invalid_topics_and_empty_candidate_fail_closed(self):
        for body,topics in [(BODY,TOPICS.replace('  ',' ')),('太短',TOPICS),(BODY+' #多余话题',TOPICS)]:
            job=self.job(body);job['drafts'][0]['topics']=topics
            self.assertEqual(self.cli(job,'commit')[0],3)

    def test_reused_angle_rejected_even_when_wording_differs(self):
        self.action(self.job(), 'import')
        result, code = self.action(self.job(OTHER), 'check')
        self.assertEqual(code,2);self.assertEqual(result['matches'][0]['reason'],'reused_angle')

    def test_paraphrased_same_plan_is_rejected(self):
        self.action(self.job(), 'import')
        job=self.job(OTHER,'同一场景换了描述')
        job['drafts'][0]['variation']=dict(scene='breakfast',focus='finding',structure='observation_choice')
        result,code=self.action(job,'check')
        self.assertEqual(code,2);self.assertEqual(result['matches'][0]['reason'],'reused_variation')

    def test_non_utf8_environment_still_outputs_utf8(self):
        import os
        result=subprocess.run([sys.executable,str(SCRIPT),'--db',str(self.db),'history','--account','账号甲','--product','棉布钥匙包'],capture_output=True,env={**os.environ,'PYTHONIOENCODING':'ascii'})
        self.assertEqual(result.returncode,0);self.assertEqual(json.loads(result.stdout.decode('utf-8'))['status'],'history_read')

    def test_import_unknown_mode_and_idempotent_receipt(self):
        job=self.job(mode=0)
        self.assertEqual(self.cli(job,'commit')[0],3)
        self.assertEqual(self.cli(job,'import')[0],0)
        code,result=self.cli(job,'import');self.assertEqual(result['status'],'already_recorded')
        job['drafts'][0]['body']=OTHER
        self.assertEqual(self.cli(job,'import')[0],3)

    def test_simultaneous_commits_only_one_wins(self):
        jobs=[self.review(self.job()) for _ in range(2)]
        processes=[]
        for i,job in enumerate(jobs):
            p=Path(self.temp.name)/f'candidate-{i}.json';p.write_text(json.dumps(job))
            processes.append(subprocess.Popen([sys.executable,str(SCRIPT),'--db',str(self.db),'commit','--input',str(p)],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True))
        outcomes=[]
        for p in processes:
            stdout,stderr=p.communicate(timeout=20);outcomes.append(p.returncode)
        self.assertEqual(sorted(outcomes),[0,2])


if __name__ == '__main__': unittest.main()
