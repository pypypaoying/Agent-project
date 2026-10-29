"""Full-column category evidence, not an inference from the sample preview."""
import hashlib
from labweaver.tools.csv_profile import profile_csv


def test_category_count_includes_labels_after_preview(tmp_path):
    path=tmp_path/'categories.csv'
    path.write_text('label,value\na,01\nb,1\na,1.0\nb,NA\na,NULL\nc,\n , \n',encoding='utf-8')
    before=hashlib.sha256(path.read_bytes()).hexdigest()
    result=profile_csv(path)
    assert result['status']=='completed'
    assert result['columns'][0]['distinct_count']==3
    assert result['columns'][1]['distinct_count']==5
    assert result['columns'][0]['missing_count']==1
    assert result['columns'][1]['missing_count']==2
    assert hashlib.sha256(path.read_bytes()).hexdigest()==before


def test_empty_and_duplicate_columns_keep_position_counts(tmp_path):
    path=tmp_path/'duplicate.csv'
    path.write_text('x,x\na,\nb, \n',encoding='utf-8')
    result=profile_csv(path)
    assert [c['distinct_count'] for c in result['columns']]==[2,0]
