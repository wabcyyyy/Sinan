-- 用户的私有旅行资料，既不发布到模板，也不作为通用 POI 语料。
CREATE TABLE itinerary_preparation (
    itinerary_id BIGINT NOT NULL PRIMARY KEY,
    revision BIGINT NOT NULL DEFAULT 0,
    data_json JSON NOT NULL,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);
