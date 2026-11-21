select c.CHNL_NM, sum(m.NET_AMT) as NET_AMT
from NX_AGG1_DSL m join LX_ZCM_CHNL c on m.CHNL_CD = c.CHNL_CD
where m.DT BETWEEN '20261001' AND '20261031'
group by c.CHNL_NM
order by 2 desc
