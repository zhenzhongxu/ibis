SELECT t0.`foo_id`, sum(t0.`f`) AS `total`
FROM star1 t0
GROUP BY 1
HAVING sum(t0.`f`) > 10