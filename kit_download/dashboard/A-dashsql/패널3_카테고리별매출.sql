select k.CAT_M, sum(m.NET_AMT) as NET_AMT
from NX_AGG1_DSL m join LX_KRP2_C k on m.CAT_CD = k.CAT_CD
where m.DT BETWEEN '20261001' AND '20261031'
group by k.CAT_M
order by 2 desc
