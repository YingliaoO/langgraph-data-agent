MERGE (c1:RetailCategory {name:'Electronics'});
MERGE (c2:RetailCategory {name:'Apparel'});
MERGE (c3:RetailCategory {name:'Sports'});
MERGE (c4:RetailCategory {name:'Home'});

MERGE (s1:RetailSupplier {supplier_id:'SUP-NORTH'}) SET s1.name='NorthStar Electronics', s1.region='East China', s1.sla_hours=48;
MERGE (s2:RetailSupplier {supplier_id:'SUP-EAST'})  SET s2.name='EastBridge Supply', s2.region='East China', s2.sla_hours=72;
MERGE (s3:RetailSupplier {supplier_id:'SUP-SOUTH'}) SET s3.name='SouthPeak Sports', s3.region='South China', s3.sla_hours=48;

MERGE (p1:RetailProduct {sku:'P1001'}) SET p1.name='Aurora ANC Headphones', p1.price=699.0;
MERGE (p2:RetailProduct {sku:'P1002'}) SET p2.name='Orbit Mechanical Keyboard', p2.price=499.0;
MERGE (p3:RetailProduct {sku:'P2001'}) SET p3.name='Linen Travel Shirt', p3.price=299.0;
MERGE (p4:RetailProduct {sku:'P3001'}) SET p4.name='Trail Running Shoes', p4.price=599.0;
MERGE (p5:RetailProduct {sku:'P4001'}) SET p5.name='BrewPro Coffee Grinder', p5.price=399.0;
MERGE (p6:RetailProduct {sku:'P5001'}) SET p6.name='PixelDesk Lamp', p6.price=259.0;

MATCH (p:RetailProduct {sku:'P1001'}), (s:RetailSupplier {supplier_id:'SUP-NORTH'}), (c:RetailCategory {name:'Electronics'}) MERGE (p)-[:SUPPLIED_BY]->(s) MERGE (p)-[:IN_CATEGORY]->(c);
MATCH (p:RetailProduct {sku:'P1002'}), (s:RetailSupplier {supplier_id:'SUP-NORTH'}), (c:RetailCategory {name:'Electronics'}) MERGE (p)-[:SUPPLIED_BY]->(s) MERGE (p)-[:IN_CATEGORY]->(c);
MATCH (p:RetailProduct {sku:'P2001'}), (s:RetailSupplier {supplier_id:'SUP-EAST'}), (c:RetailCategory {name:'Apparel'}) MERGE (p)-[:SUPPLIED_BY]->(s) MERGE (p)-[:IN_CATEGORY]->(c);
MATCH (p:RetailProduct {sku:'P3001'}), (s:RetailSupplier {supplier_id:'SUP-SOUTH'}), (c:RetailCategory {name:'Sports'}) MERGE (p)-[:SUPPLIED_BY]->(s) MERGE (p)-[:IN_CATEGORY]->(c);
MATCH (p:RetailProduct {sku:'P4001'}), (s:RetailSupplier {supplier_id:'SUP-EAST'}), (c:RetailCategory {name:'Home'}) MERGE (p)-[:SUPPLIED_BY]->(s) MERGE (p)-[:IN_CATEGORY]->(c);
MATCH (p:RetailProduct {sku:'P5001'}), (s:RetailSupplier {supplier_id:'SUP-NORTH'}), (c:RetailCategory {name:'Home'}) MERGE (p)-[:SUPPLIED_BY]->(s) MERGE (p)-[:IN_CATEGORY]->(c);

MERGE (campaign:RetailCampaign {campaign_id:'CMP-AUTUMN-2026'}) SET campaign.name='Autumn Member Week', campaign.start_date='2026-09-15', campaign.end_date='2026-09-21';
MATCH (campaign:RetailCampaign {campaign_id:'CMP-AUTUMN-2026'}), (p:RetailProduct) WHERE p.sku IN ['P1001','P2001','P3001'] MERGE (p)-[:IN_CAMPAIGN]->(campaign);
