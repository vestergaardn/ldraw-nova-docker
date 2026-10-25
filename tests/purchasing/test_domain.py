import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'web/backend'))
import pytest
from purchasing.domain import CatalogueElement, IdentityIndex, PartRequirement, build_quote, reviewed_items, requirements_from_csv


def example():
 i=IdentityIndex();i.parts['3001'].add('3001');i.colors['4'].add('4');i.lego_colors['4'].add('21');i.elements['3001','4'].add('300121')
 return i, {'300121':CatalogueElement('300121','3001','21','Brick','available',21,1000,max_quantity=5)}

def test_missing_prices_never_zero():
 i,c=example();q=build_quote('r',[PartRequirement('3001','4',2),PartRequirement('unknown','4',1)],i,c,now=1000)
 assert q['subtotal'] is None and q['known_subtotal']==42
 assert q['lines'][1]['state']=='unmapped'
 assert q['coverage']==2/3
 with pytest.raises(ValueError):reviewed_items(q,available_only=False,now=1000)
 assert reviewed_items(q,available_only=True,now=1000)==[{'elementId':'300121','quantity':2}]

def test_never_guess_colors_or_prints():
 i,c=example();q=build_quote('r',[PartRequirement('3001p01','4',1),PartRequirement('3001','21',1)],i,c,now=1000)
 assert all(l['state']=='unmapped' for l in q['lines'])
 c['300121']=CatalogueElement('300121','3001','26','Brick','available',21,1000)
 q=build_quote('r',[PartRequirement('3001','4',1)],i,c,now=1000)
 assert q['lines'][0]['issue']=='catalogue_color_mismatch'
 assert q['subtotal'] is None

def test_ambiguity_and_stale_data_block_cart():
 i,c=example();i.parts['3001'].add('3001a')
 q=build_quote('r',[PartRequirement('3001','4',1)],i,c,now=1000)
 assert q['lines'][0]['issue']=='ambiguous_part'
 i.parts['3001'].remove('3001a')
 q=build_quote('r',[PartRequirement('3001','4',1)],i,c,now=2000)
 with pytest.raises(ValueError):reviewed_items(q,available_only=True,now=2000)

def test_stock_and_limits():
 i,c=example();q=build_quote('r',[PartRequirement('3001','4',6)],i,c,now=1000)
 assert q['lines'][0]['issue']=='quantity_limit' and q['coverage']==0
 c['300121']=CatalogueElement('300121','3001','21','Brick','out_of_stock',21,1000)
 q=build_quote('r',[PartRequirement('3001','4',2)],i,c,now=1000)
 assert q['subtotal']==42 and q['coverage']==0
 with pytest.raises(ValueError):reviewed_items(q,available_only=True,now=1000)

def test_revisions_keep_price_snapshots():
 i,c=example();a=build_quote('a',[PartRequirement('3001','4',2)],i,c,now=1000)
 c['300121']=CatalogueElement('300121','3001','21','Brick','available',30,1001)
 b=build_quote('b',[PartRequirement('3001','4',3)],i,c,now=1001)
 assert a['subtotal']==42 and b['subtotal']==90 and a['id']!=b['id']

def test_csv_uses_explicit_codes_and_rejects_negative_quantities():
 text='Part ID,Color Code,Quantity,Part Name\n3001.dat,4,2,Brick\n3001.dat,4,3,Brick\n'
 assert requirements_from_csv(text)==[PartRequirement('3001','4',5,'Brick')]
 with pytest.raises(ValueError):requirements_from_csv(text.replace(',2,',',-2,'))
