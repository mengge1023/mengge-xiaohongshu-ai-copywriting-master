import copy
import importlib.util
from pathlib import Path
import tempfile
import unittest
import uuid

spec=importlib.util.spec_from_file_location('checker',Path(__file__).resolve().parents[1]/'scripts/draft_history.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
GOOD='早上赶着出门，手里还拿着早餐，这时候再翻包找钥匙，光想想就有点忙乱。我会先给钥匙定个固定位置，少让它跟包里的其他东西混在一起。\n\n这个蓝色棉布钥匙包，可以作为集中放钥匙的一个选择。我在意的是收纳位置清楚，拿完还能放回原处。至于能装多少、放进自己的包里合不合适，还是要看实际尺寸。\n\n对我来说，选这类小物件不用想得太复杂。先看看自己平时在哪里找钥匙，再决定要不要用一个专门的小包。把需要和位置想清楚，比只看颜色就下单更适合我。'
BAD='出门前整理通勤包包，钥匙总散在包底，赶时间出门掏半天，越掏越乱，本来就着急还平添不少烦躁。\n\n这个蓝色棉布钥匙包刚好能把所有钥匙都收进去，棉布摸着软乎乎的，也不占包里多少地方，现在找钥匙一下子就能摸到，不用再在包底乱翻一通。\n\n以前总觉得这类小物件可有可无，用上才发现，日常通勤这种小细节真的能提升出门幸福感，几十块钱解决每日出门的小烦躁，太值得入手了。'

def job(body=GOOD):
 return dict(request_id=str(uuid.uuid4()),account='test',product='key bag',mode=5,source_topics='#钥匙',product_facts=[dict(kind='material',value='棉布',source='构造原始资料',status='用户提供')],drafts=[dict(title='',body=body,topics='#钥匙',angle='购买前核对需要',variation=dict(scene='before_purchase',focus='purchase_criteria',structure='question_criteria_choice'))])

class Mode5Tests(unittest.TestCase):
 def test_actual_failed_model_output_is_rejected(self):
  reasons={x['reason'] for x in m.content_issues(job(BAD))}
  for kind in ['price','capacity','touch','experience','effect']:
   self.assertIn('unsupported_'+kind,reasons)
 def test_supported_selection_view_passes_format_and_fact_patterns(self):
  self.assertEqual(m.content_issues(job()),[])
 def test_invented_plan_labels_and_short_body_rejected(self):
  j=job('这一段太短，也没有按照要求分成三段。');j['drafts'][0]['variation']['scene']='新的好听标签'
  self.assertTrue({'body_length','paragraph_count','unknown_plan_id'}<={x['reason'] for x in m.content_issues(j)})
 def test_explicit_user_word_range_is_respected(self):
  j=job();j['body_length']={'min':300,'max':400}
  self.assertIn('body_length',{x['reason'] for x in m.content_issues(j)})
 def test_known_source_categories_not_treated_as_unknown(self):
  j=job(BAD)
  for k in m.CONTRACT['unsupported_claim_patterns']:
   j['product_facts'].append(dict(kind=k,value='用户提供对应真实资料',source='测试输入',status='用户提供'))
  self.assertFalse(any(x['reason'].startswith('unsupported_') for x in m.content_issues(j)))
 def test_mode5_review_requires_actual_comparison_quotes(self):
  j=job();r={'payload_hash':m.payload_hash(j),'required_review_ids':['1'],'neighbors':[{'history_id':'1','draft_index':0,'body':BAD}]}
  j['semantic_review']=dict(status='passed',payload_hash=r['payload_hash'],compared_ids=['1'],batch_reviewed=True,notes='本次测试只检查复核证据的存在与绑定。',facts_supported=True,distinct_content=True,comparison_evidence=[])
  self.assertFalse(m.semantic_pass(j,r))
  j['semantic_review']['comparison_evidence']=[dict(history_id='1',draft_index=0,old_quote=BAD[:12],new_quote=GOOD[:12],changed_dimensions=['focus','structure'])]
  self.assertTrue(m.semantic_pass(j,r))
  j['semantic_review']['comparison_evidence'][0]['old_quote']='这不是历史正文中的真实片段'
  self.assertFalse(m.semantic_pass(j,r))
 def test_bad_output_cannot_be_committed_with_passed_attestation(self):
  with tempfile.TemporaryDirectory() as t:
   db=Path(t)/'history.sqlite3';m.initialize(db);c=m.connect(db);j=job(BAD)
   j['semantic_review']={'status':'passed','facts_supported':True,'distinct_content':True}
   result,code=m.process(c,j,'commit');self.assertEqual(code,5)
   self.assertEqual(c.execute('SELECT count(*) FROM drafts').fetchone()[0],0);c.close()
 def test_observed_touch_and_timing_claims_rejected(self):
  j=job(GOOD.replace('这个蓝色棉布钥匙包','手感看着比较温和的蓝色棉布钥匙包').replace('拿完还能放回原处','拿出来再放回去也就两秒钟'))
  self.assertTrue({'unsupported_touch','unsupported_effect'} <= {x['reason'] for x in m.content_issues(j)})
 def test_reviewer_cannot_invent_history_ids(self):
  j=job();r=m.inspect(j,[])
  j['semantic_review']=dict(status='passed',payload_hash=r['payload_hash'],compared_ids=['source_original'],batch_reviewed=True,notes='复核不能把参考原文伪装成数据库里的历史稿。',facts_supported=True,distinct_content=True,comparison_evidence=[dict(history_id='source_original',draft_index=0,old_quote=BAD[:12],new_quote=GOOD[:12],changed_dimensions=['focus','structure'])])
  self.assertFalse(m.semantic_pass(j,r))

if __name__=='__main__':unittest.main()
