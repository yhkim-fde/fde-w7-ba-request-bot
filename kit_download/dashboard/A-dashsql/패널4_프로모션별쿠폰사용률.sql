select pr.PRM_ID, pr.PRM_NM, count(*) as ISS_CNT, count(u.CPN_ID) as USE_CNT,
       round(100.0 * count(u.CPN_ID) / count(*), 1) as USE_RT
from NX_BNF5_PRM pr
join NX_BNF5_MAP p on pr.PRM_ID = p.PRM_ID
join LX_BNF2_ISS i on p.CPN_ID = i.CPN_ID
left join LX_BNF3_USE u on i.CPN_ID = u.CPN_ID and i.MBR_NO = u.MBR_NO
where pr.FR_DT <= '20261031'
group by pr.PRM_ID, pr.PRM_NM
order by pr.PRM_ID
