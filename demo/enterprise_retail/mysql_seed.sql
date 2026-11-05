SET NAMES utf8mb4;
SET FOREIGN_KEY_CHECKS = 0;

CREATE TABLE IF NOT EXISTS retail_customers (
    customer_id VARCHAR(20) PRIMARY KEY,
    customer_name VARCHAR(100) NOT NULL,
    member_level VARCHAR(20) NOT NULL,
    region VARCHAR(50) NOT NULL,
    created_at DATE NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS retail_products (
    sku VARCHAR(20) PRIMARY KEY,
    product_name VARCHAR(150) NOT NULL,
    category VARCHAR(50) NOT NULL,
    supplier_id VARCHAR(20) NOT NULL,
    unit_price DECIMAL(10,2) NOT NULL,
    active TINYINT(1) NOT NULL DEFAULT 1
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS retail_orders (
    order_id VARCHAR(20) PRIMARY KEY,
    customer_id VARCHAR(20) NOT NULL,
    order_date DATE NOT NULL,
    channel VARCHAR(20) NOT NULL,
    order_status VARCHAR(20) NOT NULL,
    order_amount DECIMAL(10,2) NOT NULL,
    INDEX idx_retail_orders_customer (customer_id),
    INDEX idx_retail_orders_date (order_date),
    CONSTRAINT fk_retail_orders_customer FOREIGN KEY (customer_id) REFERENCES retail_customers(customer_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS retail_order_items (
    order_id VARCHAR(20) NOT NULL,
    sku VARCHAR(20) NOT NULL,
    quantity INT NOT NULL,
    unit_price DECIMAL(10,2) NOT NULL,
    PRIMARY KEY (order_id, sku),
    INDEX idx_retail_items_sku (sku),
    CONSTRAINT fk_retail_items_order FOREIGN KEY (order_id) REFERENCES retail_orders(order_id),
    CONSTRAINT fk_retail_items_product FOREIGN KEY (sku) REFERENCES retail_products(sku)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS retail_refunds (
    refund_id VARCHAR(20) PRIMARY KEY,
    order_id VARCHAR(20) NOT NULL,
    sku VARCHAR(20) NOT NULL,
    refund_date DATE NOT NULL,
    refund_quantity INT NOT NULL,
    reason VARCHAR(50) NOT NULL,
    refund_status VARCHAR(20) NOT NULL,
    INDEX idx_retail_refunds_order (order_id),
    INDEX idx_retail_refunds_sku (sku),
    CONSTRAINT fk_retail_refunds_order FOREIGN KEY (order_id) REFERENCES retail_orders(order_id),
    CONSTRAINT fk_retail_refunds_product FOREIGN KEY (sku) REFERENCES retail_products(sku)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

DELETE FROM retail_refunds;
DELETE FROM retail_order_items;
DELETE FROM retail_orders;
DELETE FROM retail_products;
DELETE FROM retail_customers;

INSERT INTO retail_customers VALUES
('C001','Alex Chen','Gold','East China','2026-01-08'),
('C002','Bella Wang','Silver','North China','2026-02-12'),
('C003','Chris Li','Gold','South China','2026-03-18'),
('C004','Diana Zhou','Basic','East China','2026-05-09'),
('C005','Eric Sun','Silver','South China','2026-06-21');

INSERT INTO retail_products VALUES
('P1001','Aurora ANC Headphones','Electronics','SUP-NORTH',699.00,1),
('P1002','Orbit Mechanical Keyboard','Electronics','SUP-NORTH',499.00,1),
('P2001','Linen Travel Shirt','Apparel','SUP-EAST',299.00,1),
('P3001','Trail Running Shoes','Sports','SUP-SOUTH',599.00,1),
('P4001','BrewPro Coffee Grinder','Home','SUP-EAST',399.00,1),
('P5001','PixelDesk Lamp','Home','SUP-NORTH',259.00,1);

INSERT INTO retail_orders VALUES
('O1001','C001','2026-09-01','app','completed',1398.00),
('O1002','C002','2026-09-02','web','completed',499.00),
('O1003','C003','2026-09-03','app','completed',897.00),
('O1004','C004','2026-09-05','store','completed',1198.00),
('O1005','C005','2026-09-07','web','completed',399.00),
('O1006','C001','2026-09-09','app','completed',518.00),
('O1007','C002','2026-09-11','web','completed',699.00),
('O1008','C003','2026-09-12','app','completed',1497.00),
('O1009','C004','2026-09-14','store','completed',599.00),
('O1010','C005','2026-09-16','web','completed',259.00),
('O1011','C001','2026-09-18','app','completed',798.00),
('O1012','C003','2026-09-20','web','completed',299.00);

INSERT INTO retail_order_items VALUES
('O1001','P1001',2,699.00),('O1002','P1002',1,499.00),('O1003','P2001',3,299.00),
('O1004','P3001',2,599.00),('O1005','P4001',1,399.00),('O1006','P5001',2,259.00),
('O1007','P1001',1,699.00),('O1008','P1002',3,499.00),('O1009','P3001',1,599.00),
('O1010','P5001',1,259.00),('O1011','P4001',2,399.00),('O1012','P2001',1,299.00);

INSERT INTO retail_refunds VALUES
('R9001','O1002','P1002','2026-09-06',1,'quality_defect','approved'),
('R9002','O1007','P1001','2026-09-15',1,'no_longer_needed','approved'),
('R9003','O1003','P2001','2026-09-10',1,'size_issue','approved');

SET FOREIGN_KEY_CHECKS = 1;
