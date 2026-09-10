-- Synthetic legacy schema/data, frozen from pre-D 3211c7b7830b4cf96ec04c8627750e6ec8b79a02. No production rows.
BEGIN TRANSACTION;
CREATE TABLE change_registry_annotation_revisions(
            annotation_revision_id TEXT PRIMARY KEY,
            subject_kind TEXT NOT NULL CHECK(subject_kind IN (
                'operation','change_item','fact','checkpoint','identity_incident',
                'manual_pending'
            )),
            subject_id TEXT NOT NULL,
            revision_no INTEGER NOT NULL
                CHECK(typeof(revision_no)='integer' AND revision_no>0),
            parent_revision_id TEXT REFERENCES change_registry_annotation_revisions(
                annotation_revision_id
            ),
            actor_principal TEXT NOT NULL,
            reason TEXT NOT NULL DEFAULT '',
            comment TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
                CHECK(substr(created_at,-1,1)='Z' AND julianday(created_at) IS NOT NULL),
            CHECK(length(trim(annotation_revision_id)) BETWEEN 1 AND 120),
            CHECK(length(trim(subject_id)) BETWEEN 1 AND 160),
            CHECK(length(trim(actor_principal)) BETWEEN 1 AND 160),
            CHECK(length(reason)<=1000 AND length(comment)<=4000),
            UNIQUE(subject_kind,subject_id,revision_no)
        );
CREATE TABLE change_registry_attempt_events(
            attempt_event_id TEXT PRIMARY KEY,
            attempt_id TEXT NOT NULL,
            change_item_id TEXT NOT NULL REFERENCES change_registry_items(change_item_id),
            sequence_no INTEGER NOT NULL
                CHECK(typeof(sequence_no)='integer' AND sequence_no>0),
            state TEXT NOT NULL CHECK(state IN (
                'created','submitted','confirmed','failed','rejected','cancelled',
                'ambiguous','resolved'
            )),
            resolution_state TEXT NOT NULL DEFAULT '' CHECK(
                (state='resolved' AND resolution_state IN
                    ('confirmed','failed','rejected','cancelled'))
                OR (state<>'resolved' AND resolution_state='')
            ),
            occurred_at TEXT NOT NULL
                CHECK(substr(occurred_at,-1,1)='Z' AND julianday(occurred_at) IS NOT NULL),
            receipt_reference TEXT NOT NULL DEFAULT '',
            receipt_digest TEXT NOT NULL DEFAULT '' CHECK(
                receipt_digest='' OR length(receipt_digest)=71 AND substr(receipt_digest,1,7)='sha256:'
        AND substr(receipt_digest,8) NOT GLOB '*[^0-9a-f]*'
            ),
            error_code TEXT NOT NULL DEFAULT '',
            error_message TEXT NOT NULL DEFAULT '',
            readback_proof_kind TEXT NOT NULL DEFAULT '',
            readback_digest TEXT NOT NULL DEFAULT '' CHECK(
                readback_digest='' OR length(readback_digest)=71 AND substr(readback_digest,1,7)='sha256:'
        AND substr(readback_digest,8) NOT GLOB '*[^0-9a-f]*'
            ),
            native_event_key TEXT NOT NULL DEFAULT '',
            CHECK(length(trim(attempt_event_id)) BETWEEN 1 AND 120),
            CHECK(length(trim(attempt_id)) BETWEEN 1 AND 120),
            CHECK(length(error_message)<=800),
            UNIQUE(attempt_id,sequence_no)
        );
INSERT INTO "change_registry_attempt_events" VALUES('legacy-price-created','legacy-price-attempt','legacy-price-item',1,'created','','2026-09-10T00:00:00Z','','','','','','','created');
INSERT INTO "change_registry_attempt_events" VALUES('crae_3ece37f2f79c67ac937264ef29bbc5336fd555ee484947166b8853a16536d1fe','legacy-price-attempt','legacy-price-item',2,'submitted','','2026-09-10T00:01:00Z','legacy-price','sha256:3d1bd3e918d01d73bf038d5a5ddd9f4aa6932147d4f63e0c358804f1397d9c25','','','','','submitted::legacy-price');
INSERT INTO "change_registry_attempt_events" VALUES('crae_3f0aae2271dc78cac178c6496564ace24ea0f154d48dcac95bdd9021d369ffc2','legacy-price-attempt','legacy-price-item',3,'confirmed','','2026-09-10T00:01:00Z','','','','','wb_readback','sha256:3d1bd3e918d01d73bf038d5a5ddd9f4aa6932147d4f63e0c358804f1397d9c25','confirmed:wb_readback');
INSERT INTO "change_registry_attempt_events" VALUES('legacy-bid-created','legacy-bid-attempt','legacy-bid-item',1,'created','','2026-09-10T00:00:00Z','','','','','','','created');
INSERT INTO "change_registry_attempt_events" VALUES('crae_e7e16ff59fa58d14643ce3397bf2423da1d3129002592857fbb720917662634f','legacy-bid-attempt','legacy-bid-item',2,'submitted','','2026-09-10T00:01:00Z','legacy-bid','sha256:3d1bd3e918d01d73bf038d5a5ddd9f4aa6932147d4f63e0c358804f1397d9c25','','','','','submitted::legacy-bid');
INSERT INTO "change_registry_attempt_events" VALUES('crae_70c3b6d3c2801f42d8719f72ef8b23fb197989d8847604235f25de07769ed379','legacy-bid-attempt','legacy-bid-item',3,'confirmed','','2026-09-10T00:01:00Z','','','','','wb_readback','sha256:3d1bd3e918d01d73bf038d5a5ddd9f4aa6932147d4f63e0c358804f1397d9c25','confirmed:wb_readback');
INSERT INTO "change_registry_attempt_events" VALUES('legacy-campaign-created','legacy-campaign-attempt','legacy-campaign-item',1,'created','','2026-09-10T00:00:00Z','','','','','','','created');
INSERT INTO "change_registry_attempt_events" VALUES('crae_ca370598440f609b5c0b3e67d5828451d7fef3df2416665231f35c96a04ea75a','legacy-campaign-attempt','legacy-campaign-item',2,'submitted','','2026-09-10T00:01:00Z','legacy-campaign','sha256:3d1bd3e918d01d73bf038d5a5ddd9f4aa6932147d4f63e0c358804f1397d9c25','','','','','submitted::legacy-campaign');
INSERT INTO "change_registry_attempt_events" VALUES('crae_b8f0d8da01177a716698aa75ec91fc090b9a081b4a980372e9f32fb1ebbeb08d','legacy-campaign-attempt','legacy-campaign-item',3,'confirmed','','2026-09-10T00:01:00Z','','','','','wb_readback','sha256:3d1bd3e918d01d73bf038d5a5ddd9f4aa6932147d4f63e0c358804f1397d9c25','confirmed:wb_readback');
CREATE TABLE change_registry_checkpoint_source_manifests(
            source_manifest_id TEXT PRIMARY KEY,
            checkpoint_id TEXT NOT NULL REFERENCES change_registry_checkpoints(checkpoint_id),
            source_name TEXT NOT NULL CHECK(source_name IN ('prices','ads')),
            completeness_status TEXT NOT NULL
                CHECK(completeness_status IN ('complete','partial','failed')),
            expected_count INTEGER NOT NULL CHECK(
                typeof(expected_count)='integer' AND expected_count>=0
            ),
            observed_count INTEGER NOT NULL CHECK(
                typeof(observed_count)='integer' AND observed_count>=0
                AND observed_count<=expected_count
            ),
            summary_json TEXT NOT NULL CHECK(
                json_valid(summary_json) AND json_type(summary_json)='object'
                AND length(summary_json)<=4000
            ),
            evidence_digest TEXT NOT NULL CHECK(length(evidence_digest)=71 AND substr(evidence_digest,1,7)='sha256:'
        AND substr(evidence_digest,8) NOT GLOB '*[^0-9a-f]*'),
            created_at TEXT NOT NULL
                CHECK(substr(created_at,-1,1)='Z' AND julianday(created_at) IS NOT NULL),
            CHECK(length(trim(source_manifest_id)) BETWEEN 1 AND 120),
            UNIQUE(checkpoint_id,source_name)
        );
CREATE TABLE change_registry_checkpoints(
            checkpoint_id TEXT PRIMARY KEY,
            seller_id TEXT NOT NULL,
            account_scope TEXT NOT NULL,
            source_surface TEXT NOT NULL,
            scan_kind TEXT NOT NULL
                CHECK(scan_kind IN ('observer','readback','reconciliation','manual')),
            started_at TEXT NOT NULL
                CHECK(substr(started_at,-1,1)='Z' AND julianday(started_at) IS NOT NULL),
            completed_at TEXT NOT NULL
                CHECK(substr(completed_at,-1,1)='Z' AND julianday(completed_at) IS NOT NULL),
            completeness_status TEXT NOT NULL
                CHECK(completeness_status IN ('complete','partial','failed')),
            expected_target_count INTEGER NOT NULL CHECK(
                typeof(expected_target_count)='integer' AND expected_target_count>=0
            ),
            observed_target_count INTEGER NOT NULL CHECK(
                typeof(observed_target_count)='integer' AND observed_target_count>=0
                AND observed_target_count<=expected_target_count
            ),
            completeness_digest TEXT NOT NULL CHECK(length(completeness_digest)=71 AND substr(completeness_digest,1,7)='sha256:'
        AND substr(completeness_digest,8) NOT GLOB '*[^0-9a-f]*'),
            evidence_digest TEXT NOT NULL CHECK(length(evidence_digest)=71 AND substr(evidence_digest,1,7)='sha256:'
        AND substr(evidence_digest,8) NOT GLOB '*[^0-9a-f]*'),
            previous_complete_checkpoint_id TEXT
                REFERENCES change_registry_checkpoints(checkpoint_id),
            mapping_version TEXT NOT NULL CHECK(mapping_version='wb_change_registry_mapping_v1'),
            CHECK(length(trim(checkpoint_id)) BETWEEN 1 AND 120),
            CHECK(length(trim(seller_id)) BETWEEN 1 AND 120),
            CHECK(length(trim(account_scope)) BETWEEN 1 AND 120),
            CHECK(completeness_status<>'complete'
                OR observed_target_count=expected_target_count),
            CHECK(julianday(started_at)<=julianday(completed_at)),
            UNIQUE(seller_id,account_scope,source_surface,completed_at,evidence_digest)
        );
INSERT INTO "change_registry_checkpoints" VALUES('legacy-checkpoint','legacy-seller','legacy-account','fixture','observer','2026-09-10T00:00:00Z','2026-09-10T00:01:00Z','complete',3,3,'sha256:3d1bd3e918d01d73bf038d5a5ddd9f4aa6932147d4f63e0c358804f1397d9c25','sha256:3d1bd3e918d01d73bf038d5a5ddd9f4aa6932147d4f63e0c358804f1397d9c25',NULL,'wb_change_registry_mapping_v1');
CREATE TABLE change_registry_fact_links(
            fact_link_id TEXT PRIMARY KEY,
            fact_id TEXT NOT NULL REFERENCES change_registry_facts(fact_id),
            link_kind TEXT NOT NULL CHECK(link_kind IN (
                'change_item','checkpoint','native_audit','recommendation_item'
            )),
            change_item_id TEXT REFERENCES change_registry_items(change_item_id),
            checkpoint_id TEXT REFERENCES change_registry_checkpoints(checkpoint_id),
            native_audit_reference TEXT NOT NULL DEFAULT '',
            recommendation_item_id TEXT NOT NULL DEFAULT '',
            linked_at TEXT NOT NULL
                CHECK(substr(linked_at,-1,1)='Z' AND julianday(linked_at) IS NOT NULL),
            evidence_digest TEXT NOT NULL CHECK(length(evidence_digest)=71 AND substr(evidence_digest,1,7)='sha256:'
        AND substr(evidence_digest,8) NOT GLOB '*[^0-9a-f]*'),
            CHECK(length(trim(fact_link_id)) BETWEEN 1 AND 120),
            CHECK(
                (link_kind='change_item' AND change_item_id IS NOT NULL
                    AND checkpoint_id IS NULL AND native_audit_reference=''
                    AND recommendation_item_id='')
                OR (link_kind='checkpoint' AND change_item_id IS NULL
                    AND checkpoint_id IS NOT NULL AND native_audit_reference=''
                    AND recommendation_item_id='')
                OR (link_kind='native_audit' AND change_item_id IS NULL
                    AND checkpoint_id IS NULL AND native_audit_reference<>''
                    AND recommendation_item_id='')
                OR (link_kind='recommendation_item' AND change_item_id IS NULL
                    AND checkpoint_id IS NULL AND native_audit_reference=''
                    AND recommendation_item_id<>'')
            )
        );
INSERT INTO "change_registry_fact_links" VALUES('crfl_0d621ef8958590b9d964df264d5a92d1e912d7b64c3c0dab43a0c509a7ecc714','crf_27d67f51777c24d2c300f664660ce6204b133721edde1e9043568bd603ecf4d5','change_item','legacy-price-item',NULL,'','','2026-09-10T00:01:00Z','sha256:7c80030f673bd8347e1341e93ba701b2173f724da0d34bd9b1388f4b1c89a43a');
INSERT INTO "change_registry_fact_links" VALUES('crfl_417206e2f7530823b91e2caaf6154dc258f3ba4e1f3c80b60dc6badc1b183f7c','crf_b051203858a100885b43ce21fa4f1883b146b6ed8b9086b0d80da72c8cb6baf4','change_item','legacy-bid-item',NULL,'','','2026-09-10T00:01:00Z','sha256:4ef88e32f2e2e9869909fe5a596189b9a1f4df761ce559f89d0b5b1e307440a4');
INSERT INTO "change_registry_fact_links" VALUES('crfl_9447eca70dfbfb97b2b2a0158259601c1d74fa52edecfc3d93ca8ef4606eada3','crf_a862d3bffce17e70dbfa256c877ebbf3f8440604923973ea2e21d44b7833e2b3','change_item','legacy-campaign-item',NULL,'','','2026-09-10T00:01:00Z','sha256:db55cb3dda43bde7814c9ad79ccb9d45c8ab9e1073df04a8d8d5bddc9eb58174');
CREATE TABLE change_registry_facts(
            fact_id TEXT PRIMARY KEY,
            seller_id TEXT NOT NULL,
            account_scope TEXT NOT NULL,
            target_kind TEXT NOT NULL CHECK(target_kind IN ('price','bid','campaign')),
            nm_id INTEGER NOT NULL,
            advert_id INTEGER NOT NULL DEFAULT 0,
            placement TEXT NOT NULL DEFAULT '',
            parameter_field TEXT NOT NULL CHECK(parameter_field IN (
                'original_price_minor','discount_bps','seller_price_minor',
                'bid_minor','campaign_state','payment_model','payment_unit'
            )),
            before_value_kind TEXT NOT NULL,
            before_value_integer INTEGER,
            before_value_text TEXT,
            after_value_kind TEXT NOT NULL,
            after_value_integer INTEGER,
            after_value_text TEXT,
            observed_from TEXT NOT NULL
                CHECK(substr(observed_from,-1,1)='Z' AND julianday(observed_from) IS NOT NULL),
            observed_to TEXT NOT NULL
                CHECK(substr(observed_to,-1,1)='Z' AND julianday(observed_to) IS NOT NULL),
            proven_at TEXT NOT NULL
                CHECK(substr(proven_at,-1,1)='Z' AND julianday(proven_at) IS NOT NULL),
            proof_kind TEXT NOT NULL CHECK(
                proof_kind IN ('wb_readback','native_audit','checkpoint_diff','reconciliation')
            ),
            evidence_digest TEXT NOT NULL CHECK(length(evidence_digest)=71 AND substr(evidence_digest,1,7)='sha256:'
        AND substr(evidence_digest,8) NOT GLOB '*[^0-9a-f]*'),
            mapping_version TEXT NOT NULL CHECK(mapping_version='wb_change_registry_mapping_v1'),
            CHECK(length(trim(fact_id)) BETWEEN 1 AND 120),
            CHECK(length(trim(seller_id)) BETWEEN 1 AND 120),
            CHECK(length(trim(account_scope)) BETWEEN 1 AND 120),
            CHECK(typeof(nm_id)='integer' AND nm_id>0
        AND typeof(advert_id)='integer' AND advert_id>=0
        AND (
            (target_kind='price' AND advert_id=0 AND placement=''
                AND parameter_field IN
                    ('original_price_minor','discount_bps','seller_price_minor'))
            OR (target_kind='bid' AND advert_id>0
                AND placement IN ('combined','search','recommendations')
                AND parameter_field='bid_minor')
            OR (target_kind='campaign' AND advert_id>0 AND placement=''
                AND parameter_field IN
                    ('campaign_state','payment_model','payment_unit'))
        )),
            CHECK(before_value_kind IN ('missing','null','integer','text','boolean')
        AND (
            (before_value_kind IN ('missing','null')
                AND before_value_integer IS NULL AND before_value_text IS NULL)
            OR (before_value_kind='integer' AND typeof(before_value_integer)='integer'
                AND before_value_text IS NULL)
            OR (before_value_kind='boolean' AND before_value_integer IN (0,1)
                AND before_value_text IS NULL)
            OR (before_value_kind='text' AND before_value_integer IS NULL
                AND typeof(before_value_text)='text' AND length(before_value_text)<=512)
        )),
            CHECK(after_value_kind IN ('missing','null','integer','text','boolean')
        AND (
            (after_value_kind IN ('missing','null')
                AND after_value_integer IS NULL AND after_value_text IS NULL)
            OR (after_value_kind='integer' AND typeof(after_value_integer)='integer'
                AND after_value_text IS NULL)
            OR (after_value_kind='boolean' AND after_value_integer IN (0,1)
                AND after_value_text IS NULL)
            OR (after_value_kind='text' AND after_value_integer IS NULL
                AND typeof(after_value_text)='text' AND length(after_value_text)<=512)
        )),
            CHECK((
            parameter_field IN
                ('original_price_minor','discount_bps','seller_price_minor','bid_minor')
            AND before_value_kind IN ('missing','null','integer')
            AND (before_value_kind<>'integer' OR before_value_integer>=0)
            AND (parameter_field<>'discount_bps'
                OR before_value_kind<>'integer' OR before_value_integer<=10000)
        ) OR (
            parameter_field IN ('campaign_state','payment_model','payment_unit')
            AND before_value_kind IN ('missing','null','text')
            AND (before_value_kind<>'text' OR (
                length(before_value_text) BETWEEN 1 AND 120
                AND trim(before_value_text)=before_value_text
                AND lower(before_value_text)=before_value_text
                AND before_value_text NOT GLOB '*[^a-z0-9_:-]*'
            ))
        )),
            CHECK((
            parameter_field IN
                ('original_price_minor','discount_bps','seller_price_minor','bid_minor')
            AND after_value_kind IN ('integer')
            AND (after_value_kind<>'integer' OR after_value_integer>=0)
            AND (parameter_field<>'discount_bps'
                OR after_value_kind<>'integer' OR after_value_integer<=10000)
        ) OR (
            parameter_field IN ('campaign_state','payment_model','payment_unit')
            AND after_value_kind IN ('text')
            AND (after_value_kind<>'text' OR (
                length(after_value_text) BETWEEN 1 AND 120
                AND trim(after_value_text)=after_value_text
                AND lower(after_value_text)=after_value_text
                AND after_value_text NOT GLOB '*[^a-z0-9_:-]*'
            ))
        )),
            CHECK(before_value_kind<>after_value_kind
                OR before_value_integer IS NOT after_value_integer
                OR before_value_text IS NOT after_value_text),
            CHECK(julianday(observed_from)<=julianday(observed_to)),
            CHECK(julianday(observed_to)<=julianday(proven_at)),
            UNIQUE(
                seller_id,account_scope,target_kind,nm_id,advert_id,placement,
                parameter_field,observed_from,observed_to,proof_kind,evidence_digest
            )
        );
INSERT INTO "change_registry_facts" VALUES('crf_27d67f51777c24d2c300f664660ce6204b133721edde1e9043568bd603ecf4d5','legacy-seller','legacy-account','price',101,0,'','original_price_minor','integer',100,NULL,'integer',200,NULL,'2026-09-10T00:00:00Z','2026-09-10T00:01:00Z','2026-09-10T00:01:00Z','wb_readback','sha256:27d67f51777c24d2c300f664660ce6204b133721edde1e9043568bd603ecf4d5','wb_change_registry_mapping_v1');
INSERT INTO "change_registry_facts" VALUES('crf_b051203858a100885b43ce21fa4f1883b146b6ed8b9086b0d80da72c8cb6baf4','legacy-seller','legacy-account','bid',101,99,'search','bid_minor','integer',100,NULL,'integer',200,NULL,'2026-09-10T00:00:00Z','2026-09-10T00:01:00Z','2026-09-10T00:01:00Z','wb_readback','sha256:b051203858a100885b43ce21fa4f1883b146b6ed8b9086b0d80da72c8cb6baf4','wb_change_registry_mapping_v1');
INSERT INTO "change_registry_facts" VALUES('crf_a862d3bffce17e70dbfa256c877ebbf3f8440604923973ea2e21d44b7833e2b3','legacy-seller','legacy-account','campaign',101,99,'','campaign_state','text',NULL,'paused','text',NULL,'active','2026-09-10T00:00:00Z','2026-09-10T00:01:00Z','2026-09-10T00:01:00Z','wb_readback','sha256:a862d3bffce17e70dbfa256c877ebbf3f8440604923973ea2e21d44b7833e2b3','wb_change_registry_mapping_v1');
CREATE TABLE change_registry_identity_incidents(
            incident_id TEXT PRIMARY KEY,
            seller_id TEXT NOT NULL,
            account_scope TEXT NOT NULL,
            incident_kind TEXT NOT NULL CHECK(incident_kind IN (
                'campaign_nm_mapping_cardinality','invalid_target_identity','identity_drift'
            )),
            target_kind TEXT NOT NULL CHECK(target_kind IN ('price','bid','campaign')),
            advert_id INTEGER NOT NULL DEFAULT 0 CHECK(
                typeof(advert_id)='integer' AND advert_id>=0
            ),
            candidate_nm_ids_json TEXT NOT NULL CHECK(
                json_valid(candidate_nm_ids_json)
                AND json_type(candidate_nm_ids_json)='array'
            ),
            candidate_count INTEGER NOT NULL CHECK(
                typeof(candidate_count)='integer' AND candidate_count>=0
                AND candidate_count=json_array_length(candidate_nm_ids_json)
            ),
            source_surface TEXT NOT NULL,
            observed_at TEXT NOT NULL
                CHECK(substr(observed_at,-1,1)='Z' AND julianday(observed_at) IS NOT NULL),
            evidence_digest TEXT NOT NULL CHECK(length(evidence_digest)=71 AND substr(evidence_digest,1,7)='sha256:'
        AND substr(evidence_digest,8) NOT GLOB '*[^0-9a-f]*'),
            mapping_version TEXT NOT NULL CHECK(mapping_version='wb_change_registry_mapping_v1'),
            CHECK(length(trim(incident_id)) BETWEEN 1 AND 120),
            CHECK(length(trim(seller_id)) BETWEEN 1 AND 120),
            CHECK(length(trim(account_scope)) BETWEEN 1 AND 120),
            CHECK(incident_kind<>'campaign_nm_mapping_cardinality'
                OR (target_kind='campaign' AND advert_id>0 AND candidate_count<>1)),
            UNIQUE(
                seller_id,account_scope,incident_kind,target_kind,advert_id,
                observed_at,evidence_digest
            )
        );
CREATE TABLE change_registry_items(
            change_item_id TEXT PRIMARY KEY,
            operation_id TEXT NOT NULL,
            seller_id TEXT NOT NULL,
            account_scope TEXT NOT NULL,
            target_kind TEXT NOT NULL CHECK(target_kind IN ('price','bid','campaign')),
            nm_id INTEGER NOT NULL,
            advert_id INTEGER NOT NULL DEFAULT 0,
            placement TEXT NOT NULL DEFAULT '',
            parameter_field TEXT NOT NULL CHECK(parameter_field IN (
                'original_price_minor','discount_bps','seller_price_minor',
                'bid_minor','campaign_state','payment_model','payment_unit'
            )),
            before_value_kind TEXT NOT NULL,
            before_value_integer INTEGER,
            before_value_text TEXT,
            requested_value_kind TEXT NOT NULL,
            requested_value_integer INTEGER,
            requested_value_text TEXT,
            recommendation_item_id TEXT NOT NULL DEFAULT '',
            mapping_version TEXT NOT NULL CHECK(mapping_version='wb_change_registry_mapping_v1'),
            created_at TEXT NOT NULL
                CHECK(substr(created_at,-1,1)='Z' AND julianday(created_at) IS NOT NULL),
            FOREIGN KEY(operation_id,seller_id,account_scope)
                REFERENCES change_registry_operations(operation_id,seller_id,account_scope),
            CHECK(length(trim(change_item_id)) BETWEEN 1 AND 120),
            CHECK(typeof(nm_id)='integer' AND nm_id>0
        AND typeof(advert_id)='integer' AND advert_id>=0
        AND (
            (target_kind='price' AND advert_id=0 AND placement=''
                AND parameter_field IN
                    ('original_price_minor','discount_bps','seller_price_minor'))
            OR (target_kind='bid' AND advert_id>0
                AND placement IN ('combined','search','recommendations')
                AND parameter_field='bid_minor')
            OR (target_kind='campaign' AND advert_id>0 AND placement=''
                AND parameter_field IN
                    ('campaign_state','payment_model','payment_unit'))
        )),
            CHECK(before_value_kind IN ('missing','null','integer','text','boolean')
        AND (
            (before_value_kind IN ('missing','null')
                AND before_value_integer IS NULL AND before_value_text IS NULL)
            OR (before_value_kind='integer' AND typeof(before_value_integer)='integer'
                AND before_value_text IS NULL)
            OR (before_value_kind='boolean' AND before_value_integer IN (0,1)
                AND before_value_text IS NULL)
            OR (before_value_kind='text' AND before_value_integer IS NULL
                AND typeof(before_value_text)='text' AND length(before_value_text)<=512)
        )),
            CHECK(requested_value_kind IN ('missing','null','integer','text','boolean')
        AND (
            (requested_value_kind IN ('missing','null')
                AND requested_value_integer IS NULL AND requested_value_text IS NULL)
            OR (requested_value_kind='integer' AND typeof(requested_value_integer)='integer'
                AND requested_value_text IS NULL)
            OR (requested_value_kind='boolean' AND requested_value_integer IN (0,1)
                AND requested_value_text IS NULL)
            OR (requested_value_kind='text' AND requested_value_integer IS NULL
                AND typeof(requested_value_text)='text' AND length(requested_value_text)<=512)
        )),
            CHECK((
            parameter_field IN
                ('original_price_minor','discount_bps','seller_price_minor','bid_minor')
            AND before_value_kind IN ('missing','null','integer')
            AND (before_value_kind<>'integer' OR before_value_integer>=0)
            AND (parameter_field<>'discount_bps'
                OR before_value_kind<>'integer' OR before_value_integer<=10000)
        ) OR (
            parameter_field IN ('campaign_state','payment_model','payment_unit')
            AND before_value_kind IN ('missing','null','text')
            AND (before_value_kind<>'text' OR (
                length(before_value_text) BETWEEN 1 AND 120
                AND trim(before_value_text)=before_value_text
                AND lower(before_value_text)=before_value_text
                AND before_value_text NOT GLOB '*[^a-z0-9_:-]*'
            ))
        )),
            CHECK((
            parameter_field IN
                ('original_price_minor','discount_bps','seller_price_minor','bid_minor')
            AND requested_value_kind IN ('integer')
            AND (requested_value_kind<>'integer' OR requested_value_integer>=0)
            AND (parameter_field<>'discount_bps'
                OR requested_value_kind<>'integer' OR requested_value_integer<=10000)
        ) OR (
            parameter_field IN ('campaign_state','payment_model','payment_unit')
            AND requested_value_kind IN ('text')
            AND (requested_value_kind<>'text' OR (
                length(requested_value_text) BETWEEN 1 AND 120
                AND trim(requested_value_text)=requested_value_text
                AND lower(requested_value_text)=requested_value_text
                AND requested_value_text NOT GLOB '*[^a-z0-9_:-]*'
            ))
        )),
            UNIQUE(operation_id,target_kind,nm_id,advert_id,placement,parameter_field)
        );
INSERT INTO "change_registry_items" VALUES('legacy-price-item','legacy-price','legacy-seller','legacy-account','price',101,0,'','original_price_minor','integer',100,NULL,'integer',200,NULL,'','wb_change_registry_mapping_v1','2026-09-10T00:00:00Z');
INSERT INTO "change_registry_items" VALUES('legacy-bid-item','legacy-bid','legacy-seller','legacy-account','bid',101,99,'search','bid_minor','integer',100,NULL,'integer',200,NULL,'','wb_change_registry_mapping_v1','2026-09-10T00:00:00Z');
INSERT INTO "change_registry_items" VALUES('legacy-campaign-item','legacy-campaign','legacy-seller','legacy-account','campaign',101,99,'','campaign_state','text',NULL,'paused','text',NULL,'active','','wb_change_registry_mapping_v1','2026-09-10T00:00:00Z');
CREATE TABLE change_registry_manual_pending_current(
            seller_id TEXT NOT NULL,
            account_scope TEXT NOT NULL,
            target_kind TEXT NOT NULL CHECK(target_kind IN ('price','bid','campaign')),
            nm_id INTEGER NOT NULL,
            advert_id INTEGER NOT NULL DEFAULT 0,
            placement TEXT NOT NULL DEFAULT '',
            parameter_field TEXT NOT NULL CHECK(parameter_field IN (
                'original_price_minor','discount_bps','seller_price_minor',
                'bid_minor','campaign_state','payment_model','payment_unit'
            )),
            current_pending_id TEXT NOT NULL,
            current_event_id TEXT NOT NULL
                REFERENCES change_registry_manual_pending_events(pending_event_id),
            active INTEGER NOT NULL CHECK(active IN (0,1)),
            revision INTEGER NOT NULL
                CHECK(typeof(revision)='integer' AND revision>0),
            updated_at TEXT NOT NULL
                CHECK(substr(updated_at,-1,1)='Z' AND julianday(updated_at) IS NOT NULL),
            CHECK(length(trim(seller_id)) BETWEEN 1 AND 120),
            CHECK(length(trim(account_scope)) BETWEEN 1 AND 120),
            CHECK(length(trim(current_pending_id)) BETWEEN 1 AND 120),
            CHECK(typeof(nm_id)='integer' AND nm_id>0
        AND typeof(advert_id)='integer' AND advert_id>=0
        AND (
            (target_kind='price' AND advert_id=0 AND placement=''
                AND parameter_field IN
                    ('original_price_minor','discount_bps','seller_price_minor'))
            OR (target_kind='bid' AND advert_id>0
                AND placement IN ('combined','search','recommendations')
                AND parameter_field='bid_minor')
            OR (target_kind='campaign' AND advert_id>0 AND placement=''
                AND parameter_field IN
                    ('campaign_state','payment_model','payment_unit'))
        )),
            PRIMARY KEY(
                seller_id,account_scope,target_kind,nm_id,advert_id,placement,
                parameter_field
            )
        );
CREATE TABLE change_registry_manual_pending_events(
            pending_event_id TEXT PRIMARY KEY,
            pending_id TEXT NOT NULL,
            change_item_id TEXT NOT NULL REFERENCES change_registry_items(change_item_id),
            sequence_no INTEGER NOT NULL
                CHECK(typeof(sequence_no)='integer' AND sequence_no>0),
            state TEXT NOT NULL CHECK(state IN (
                'pending','superseded','matched','deviated','expired'
            )),
            related_fact_id TEXT REFERENCES change_registry_facts(fact_id),
            supersedes_pending_id TEXT NOT NULL DEFAULT '',
            occurred_at TEXT NOT NULL
                CHECK(substr(occurred_at,-1,1)='Z' AND julianday(occurred_at) IS NOT NULL),
            evidence_digest TEXT NOT NULL CHECK(length(evidence_digest)=71 AND substr(evidence_digest,1,7)='sha256:'
        AND substr(evidence_digest,8) NOT GLOB '*[^0-9a-f]*'),
            native_event_key TEXT NOT NULL DEFAULT '',
            CHECK(length(trim(pending_event_id)) BETWEEN 1 AND 120),
            CHECK(length(trim(pending_id)) BETWEEN 1 AND 120),
            CHECK((state IN ('matched','deviated') AND related_fact_id IS NOT NULL)
                OR (state NOT IN ('matched','deviated') AND related_fact_id IS NULL)),
            UNIQUE(pending_id,sequence_no)
        );
CREATE TABLE change_registry_observation_values(
            observation_value_id TEXT PRIMARY KEY,
            checkpoint_id TEXT NOT NULL REFERENCES change_registry_checkpoints(checkpoint_id),
            target_kind TEXT NOT NULL CHECK(target_kind IN ('price','bid','campaign')),
            nm_id INTEGER NOT NULL,
            advert_id INTEGER NOT NULL DEFAULT 0,
            placement TEXT NOT NULL DEFAULT '',
            parameter_field TEXT NOT NULL CHECK(parameter_field IN (
                'original_price_minor','discount_bps','seller_price_minor',
                'bid_minor','campaign_state','payment_model','payment_unit'
            )),
            observation_status TEXT NOT NULL CHECK(observation_status IN (
                'exact','exact_zero','missing','inapplicable','error'
            )),
            value_kind TEXT NOT NULL,
            value_integer INTEGER,
            value_text TEXT,
            health_code TEXT NOT NULL DEFAULT '',
            health_detail TEXT NOT NULL DEFAULT '',
            observed_at TEXT NOT NULL
                CHECK(substr(observed_at,-1,1)='Z' AND julianday(observed_at) IS NOT NULL),
            evidence_digest TEXT NOT NULL CHECK(length(evidence_digest)=71 AND substr(evidence_digest,1,7)='sha256:'
        AND substr(evidence_digest,8) NOT GLOB '*[^0-9a-f]*'),
            mapping_version TEXT NOT NULL CHECK(mapping_version='wb_change_registry_mapping_v1'),
            CHECK(length(trim(observation_value_id)) BETWEEN 1 AND 120),
            CHECK(typeof(nm_id)='integer' AND nm_id>0
        AND typeof(advert_id)='integer' AND advert_id>=0
        AND (
            (target_kind='price' AND advert_id=0 AND placement=''
                AND parameter_field IN
                    ('original_price_minor','discount_bps','seller_price_minor'))
            OR (target_kind='bid' AND advert_id>0
                AND placement IN ('combined','search','recommendations')
                AND parameter_field='bid_minor')
            OR (target_kind='campaign' AND advert_id>0 AND placement=''
                AND parameter_field IN
                    ('campaign_state','payment_model','payment_unit'))
        )),
            CHECK(value_kind IN ('missing','null','integer','text','boolean')
        AND (
            (value_kind IN ('missing','null')
                AND value_integer IS NULL AND value_text IS NULL)
            OR (value_kind='integer' AND typeof(value_integer)='integer'
                AND value_text IS NULL)
            OR (value_kind='boolean' AND value_integer IN (0,1)
                AND value_text IS NULL)
            OR (value_kind='text' AND value_integer IS NULL
                AND typeof(value_text)='text' AND length(value_text)<=512)
        )),
            CHECK((
            parameter_field IN
                ('original_price_minor','discount_bps','seller_price_minor','bid_minor')
            AND value_kind IN ('missing','null','integer')
            AND (value_kind<>'integer' OR value_integer>=0)
            AND (parameter_field<>'discount_bps'
                OR value_kind<>'integer' OR value_integer<=10000)
        ) OR (
            parameter_field IN ('campaign_state','payment_model','payment_unit')
            AND value_kind IN ('missing','null','text')
            AND (value_kind<>'text' OR (
                length(value_text) BETWEEN 1 AND 120
                AND trim(value_text)=value_text
                AND lower(value_text)=value_text
                AND value_text NOT GLOB '*[^a-z0-9_:-]*'
            ))
        )),
            CHECK(
                (observation_status='exact' AND value_kind<>'missing')
                OR (observation_status='exact_zero' AND value_kind='integer'
                    AND value_integer=0)
                OR (observation_status IN ('missing','inapplicable','error')
                    AND value_kind='missing')
            ),
            CHECK(length(health_detail)<=800),
            UNIQUE(
                checkpoint_id,target_kind,nm_id,advert_id,placement,parameter_field
            )
        );
CREATE TABLE change_registry_observer_health_events(
            health_event_id TEXT PRIMARY KEY,
            seller_id TEXT NOT NULL,
            account_scope TEXT NOT NULL,
            scheduled_slot TEXT NOT NULL,
            outcome TEXT NOT NULL CHECK(outcome IN ('complete','partial','failed')),
            consecutive_noncomplete INTEGER NOT NULL CHECK(
                typeof(consecutive_noncomplete)='integer' AND consecutive_noncomplete>=0
            ),
            health_state TEXT NOT NULL CHECK(health_state IN ('normal','degraded')),
            job_id TEXT NOT NULL REFERENCES change_registry_observer_jobs(job_id),
            checkpoint_id TEXT REFERENCES change_registry_checkpoints(checkpoint_id),
            occurred_at TEXT NOT NULL
                CHECK(substr(occurred_at,-1,1)='Z' AND julianday(occurred_at) IS NOT NULL),
            evidence_digest TEXT NOT NULL CHECK(length(evidence_digest)=71 AND substr(evidence_digest,1,7)='sha256:'
        AND substr(evidence_digest,8) NOT GLOB '*[^0-9a-f]*'),
            CHECK(length(trim(health_event_id)) BETWEEN 1 AND 120),
            UNIQUE(seller_id,account_scope,scheduled_slot)
        );
CREATE TABLE change_registry_observer_job_events(
            job_event_id TEXT PRIMARY KEY,
            job_id TEXT NOT NULL REFERENCES change_registry_observer_jobs(job_id),
            sequence_no INTEGER NOT NULL CHECK(
                typeof(sequence_no)='integer' AND sequence_no>0
            ),
            state TEXT NOT NULL CHECK(state IN (
                'accepted','running','complete','partial','failed','busy'
            )),
            occurred_at TEXT NOT NULL
                CHECK(substr(occurred_at,-1,1)='Z' AND julianday(occurred_at) IS NOT NULL),
            checkpoint_id TEXT REFERENCES change_registry_checkpoints(checkpoint_id),
            fact_count INTEGER NOT NULL DEFAULT 0 CHECK(
                typeof(fact_count)='integer' AND fact_count>=0
            ),
            error_code TEXT NOT NULL DEFAULT '',
            error_message TEXT NOT NULL DEFAULT '' CHECK(length(error_message)<=800),
            source_status TEXT NOT NULL DEFAULT 'not_observed' CHECK(
                source_status IN ('not_observed','complete','partial','failed','invalid')
            ),
            failure_origin TEXT NOT NULL DEFAULT '' CHECK(
                failure_origin IN ('','source_acquisition','local_persistence')
            ),
            persistence_stage TEXT NOT NULL DEFAULT '' CHECK(length(persistence_stage)<=80),
            persistence_table TEXT NOT NULL DEFAULT '' CHECK(length(persistence_table)<=160),
            persistence_operation TEXT NOT NULL DEFAULT '' CHECK(length(persistence_operation)<=160),
            sqlite_errorcode INTEGER CHECK(
                sqlite_errorcode IS NULL OR (
                    typeof(sqlite_errorcode)='integer'
                    AND sqlite_errorcode>=0 AND sqlite_errorcode<=65535
                )
            ),
            sqlite_errorname TEXT NOT NULL DEFAULT '' CHECK(length(sqlite_errorname)<=80),
            constraint_category TEXT NOT NULL DEFAULT '' CHECK(length(constraint_category)<=80),
            constraint_name TEXT NOT NULL DEFAULT '' CHECK(length(constraint_name)<=320),
            error_digest TEXT NOT NULL DEFAULT '' CHECK(
                error_digest='' OR length(error_digest)=71 AND substr(error_digest,1,7)='sha256:'
        AND substr(error_digest,8) NOT GLOB '*[^0-9a-f]*'
            ),
            fallback_persistence_stage TEXT NOT NULL DEFAULT '' CHECK(
                length(fallback_persistence_stage)<=80
            ),
            fallback_persistence_table TEXT NOT NULL DEFAULT '' CHECK(
                length(fallback_persistence_table)<=160
            ),
            fallback_persistence_operation TEXT NOT NULL DEFAULT '' CHECK(
                length(fallback_persistence_operation)<=160
            ),
            fallback_error_code TEXT NOT NULL DEFAULT '' CHECK(length(fallback_error_code)<=120),
            fallback_error_message TEXT NOT NULL DEFAULT '' CHECK(length(fallback_error_message)<=800),
            fallback_sqlite_errorcode INTEGER CHECK(
                fallback_sqlite_errorcode IS NULL OR (
                    typeof(fallback_sqlite_errorcode)='integer'
                    AND fallback_sqlite_errorcode>=0
                    AND fallback_sqlite_errorcode<=65535
                )
            ),
            fallback_sqlite_errorname TEXT NOT NULL DEFAULT '' CHECK(
                length(fallback_sqlite_errorname)<=80
            ),
            fallback_constraint_category TEXT NOT NULL DEFAULT '' CHECK(
                length(fallback_constraint_category)<=80
            ),
            fallback_constraint_name TEXT NOT NULL DEFAULT '' CHECK(
                length(fallback_constraint_name)<=320
            ),
            fallback_error_digest TEXT NOT NULL DEFAULT '' CHECK(
                fallback_error_digest='' OR length(fallback_error_digest)=71 AND substr(fallback_error_digest,1,7)='sha256:'
        AND substr(fallback_error_digest,8) NOT GLOB '*[^0-9a-f]*'
            ),
            evidence_digest TEXT NOT NULL CHECK(length(evidence_digest)=71 AND substr(evidence_digest,1,7)='sha256:'
        AND substr(evidence_digest,8) NOT GLOB '*[^0-9a-f]*'),
            CHECK(length(trim(job_event_id)) BETWEEN 1 AND 120),
            UNIQUE(job_id,sequence_no)
        );
CREATE TABLE change_registry_observer_jobs(
            job_id TEXT PRIMARY KEY,
            seller_id TEXT NOT NULL,
            account_scope TEXT NOT NULL,
            trigger_kind TEXT NOT NULL CHECK(trigger_kind IN ('scheduled','manual','activation')),
            scheduled_slot TEXT NOT NULL DEFAULT '',
            requested_by TEXT NOT NULL,
            requested_at TEXT NOT NULL
                CHECK(substr(requested_at,-1,1)='Z' AND julianday(requested_at) IS NOT NULL),
            request_digest TEXT NOT NULL CHECK(length(request_digest)=71 AND substr(request_digest,1,7)='sha256:'
        AND substr(request_digest,8) NOT GLOB '*[^0-9a-f]*'),
            CHECK(length(trim(job_id)) BETWEEN 1 AND 120),
            CHECK(length(trim(seller_id)) BETWEEN 1 AND 120),
            CHECK(length(trim(account_scope)) BETWEEN 1 AND 120),
            CHECK(length(trim(requested_by)) BETWEEN 1 AND 160),
            CHECK((trigger_kind='scheduled' AND scheduled_slot<>'')
                OR (trigger_kind<>'scheduled' AND scheduled_slot=''))
        );
CREATE TABLE change_registry_observer_leases(
            seller_id TEXT NOT NULL,
            account_scope TEXT NOT NULL,
            owner_job_id TEXT NOT NULL DEFAULT '',
            acquired_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            revision INTEGER NOT NULL CHECK(typeof(revision)='integer' AND revision>0),
            updated_at TEXT NOT NULL
                CHECK(substr(updated_at,-1,1)='Z' AND julianday(updated_at) IS NOT NULL),
            CHECK(length(trim(seller_id)) BETWEEN 1 AND 120),
            CHECK(length(trim(account_scope)) BETWEEN 1 AND 120),
            CHECK((owner_job_id='' AND acquired_at='' AND expires_at='') OR (
                owner_job_id<>''
                AND substr(acquired_at,-1,1)='Z' AND julianday(acquired_at) IS NOT NULL
                AND substr(expires_at,-1,1)='Z' AND julianday(expires_at) IS NOT NULL
                AND julianday(acquired_at)<julianday(expires_at)
            )),
            PRIMARY KEY(seller_id,account_scope)
        );
CREATE TABLE change_registry_operations(
            operation_id TEXT PRIMARY KEY,
            seller_id TEXT NOT NULL,
            account_scope TEXT NOT NULL,
            source_surface TEXT NOT NULL,
            actor_principal TEXT NOT NULL,
            actor_kind TEXT NOT NULL
                CHECK(actor_kind IN ('human','service','system','import')),
            requested_at TEXT NOT NULL
                CHECK(substr(requested_at,-1,1)='Z' AND julianday(requested_at) IS NOT NULL),
            created_at TEXT NOT NULL
                CHECK(substr(created_at,-1,1)='Z' AND julianday(created_at) IS NOT NULL),
            native_idempotency_key TEXT NOT NULL DEFAULT '',
            correlation_id TEXT NOT NULL DEFAULT '',
            calculation_id TEXT NOT NULL DEFAULT '',
            apply_operation_id TEXT NOT NULL DEFAULT '',
            provenance_digest TEXT NOT NULL CHECK(length(provenance_digest)=71 AND substr(provenance_digest,1,7)='sha256:'
        AND substr(provenance_digest,8) NOT GLOB '*[^0-9a-f]*'),
            mapping_version TEXT NOT NULL CHECK(mapping_version='wb_change_registry_mapping_v1'),
            CHECK(length(trim(operation_id)) BETWEEN 1 AND 120),
            CHECK(length(trim(seller_id)) BETWEEN 1 AND 120),
            CHECK(length(trim(account_scope)) BETWEEN 1 AND 120),
            CHECK(length(trim(source_surface)) BETWEEN 1 AND 120),
            CHECK(length(trim(actor_principal)) BETWEEN 1 AND 160),
            CHECK(julianday(requested_at)<=julianday(created_at)),
            UNIQUE(operation_id,seller_id,account_scope)
        );
INSERT INTO "change_registry_operations" VALUES('legacy-price','legacy-seller','legacy-account','legacy-fixture','owner','human','2026-09-10T00:00:00Z','2026-09-10T00:00:00Z','','','','','sha256:3d1bd3e918d01d73bf038d5a5ddd9f4aa6932147d4f63e0c358804f1397d9c25','wb_change_registry_mapping_v1');
INSERT INTO "change_registry_operations" VALUES('legacy-bid','legacy-seller','legacy-account','legacy-fixture','owner','human','2026-09-10T00:00:00Z','2026-09-10T00:00:00Z','','','','','sha256:3d1bd3e918d01d73bf038d5a5ddd9f4aa6932147d4f63e0c358804f1397d9c25','wb_change_registry_mapping_v1');
INSERT INTO "change_registry_operations" VALUES('legacy-campaign','legacy-seller','legacy-account','legacy-fixture','owner','human','2026-09-10T00:00:00Z','2026-09-10T00:00:00Z','','','','','sha256:3d1bd3e918d01d73bf038d5a5ddd9f4aa6932147d4f63e0c358804f1397d9c25','wb_change_registry_mapping_v1');
CREATE UNIQUE INDEX change_registry_operations_native_idempotency
        ON change_registry_operations(seller_id,account_scope,source_surface,native_idempotency_key)
        WHERE native_idempotency_key<>'';
CREATE INDEX change_registry_operations_by_scope_time
        ON change_registry_operations(seller_id,account_scope,created_at,operation_id);
CREATE INDEX change_registry_items_by_target
        ON change_registry_items(
            seller_id,account_scope,target_kind,nm_id,advert_id,placement,
            parameter_field,created_at,change_item_id
        );
CREATE UNIQUE INDEX change_registry_attempt_events_native_key
        ON change_registry_attempt_events(change_item_id,native_event_key)
        WHERE native_event_key<>'';
CREATE INDEX change_registry_attempt_events_by_item
        ON change_registry_attempt_events(change_item_id,occurred_at,attempt_id,sequence_no);
CREATE INDEX change_registry_checkpoints_by_scope_time
        ON change_registry_checkpoints(
            seller_id,account_scope,source_surface,completed_at,checkpoint_id
        );
CREATE INDEX change_registry_facts_by_target_interval
        ON change_registry_facts(
            seller_id,account_scope,target_kind,nm_id,advert_id,placement,
            parameter_field,observed_from,observed_to,fact_id
        );
CREATE INDEX change_registry_facts_by_proven_time
        ON change_registry_facts(seller_id,account_scope,proven_at,fact_id);
CREATE INDEX change_registry_observations_by_target
        ON change_registry_observation_values(
            target_kind,nm_id,advert_id,placement,parameter_field,
            observed_at,observation_value_id
        );
CREATE TRIGGER change_registry_observation_within_checkpoint
        BEFORE INSERT ON change_registry_observation_values
        WHEN NOT EXISTS(
            SELECT 1 FROM change_registry_checkpoints checkpoint
            WHERE checkpoint.checkpoint_id=NEW.checkpoint_id
              AND julianday(checkpoint.started_at)<=julianday(NEW.observed_at)
              AND julianday(NEW.observed_at)<=julianday(checkpoint.completed_at)
        )
        BEGIN
            SELECT RAISE(ABORT,'observation timestamp is outside checkpoint interval');
        END;
CREATE INDEX change_registry_identity_incidents_by_scope_time
        ON change_registry_identity_incidents(
            seller_id,account_scope,observed_at,incident_id
        );
CREATE UNIQUE INDEX change_registry_fact_links_change_item
        ON change_registry_fact_links(fact_id,change_item_id)
        WHERE link_kind='change_item';
CREATE UNIQUE INDEX change_registry_fact_links_checkpoint
        ON change_registry_fact_links(fact_id,checkpoint_id)
        WHERE link_kind='checkpoint';
CREATE UNIQUE INDEX change_registry_fact_links_native_audit
        ON change_registry_fact_links(fact_id,native_audit_reference)
        WHERE link_kind='native_audit';
CREATE UNIQUE INDEX change_registry_fact_links_recommendation
        ON change_registry_fact_links(fact_id,recommendation_item_id)
        WHERE link_kind='recommendation_item';
CREATE INDEX change_registry_fact_links_by_fact_time
        ON change_registry_fact_links(fact_id,linked_at,fact_link_id);
CREATE TRIGGER change_registry_fact_link_exact_scope
        BEFORE INSERT ON change_registry_fact_links
        BEGIN
            SELECT CASE WHEN NEW.link_kind='change_item' AND NOT EXISTS(
                SELECT 1
                FROM change_registry_facts fact
                JOIN change_registry_items item ON item.change_item_id=NEW.change_item_id
                WHERE fact.fact_id=NEW.fact_id
                  AND fact.seller_id=item.seller_id
                  AND fact.account_scope=item.account_scope
                  AND fact.target_kind=item.target_kind
                  AND fact.nm_id=item.nm_id
                  AND fact.advert_id=item.advert_id
                  AND fact.placement=item.placement
                  AND fact.parameter_field=item.parameter_field
            ) THEN RAISE(ABORT,'fact link target identity mismatch') END;
            SELECT CASE WHEN NEW.link_kind='checkpoint' AND NOT EXISTS(
                SELECT 1
                FROM change_registry_facts fact
                JOIN change_registry_checkpoints checkpoint
                  ON checkpoint.checkpoint_id=NEW.checkpoint_id
                WHERE fact.fact_id=NEW.fact_id
                  AND fact.seller_id=checkpoint.seller_id
                  AND fact.account_scope=checkpoint.account_scope
            ) THEN RAISE(ABORT,'fact link checkpoint scope mismatch') END;
        END;
CREATE UNIQUE INDEX change_registry_annotation_parent_child
        ON change_registry_annotation_revisions(parent_revision_id)
        WHERE parent_revision_id IS NOT NULL;
CREATE INDEX change_registry_annotations_by_subject
        ON change_registry_annotation_revisions(
            subject_kind,subject_id,revision_no,annotation_revision_id
        );
CREATE INDEX change_registry_source_manifests_by_checkpoint
        ON change_registry_checkpoint_source_manifests(checkpoint_id,source_name);
CREATE UNIQUE INDEX change_registry_observer_scheduled_slot
        ON change_registry_observer_jobs(seller_id,account_scope,scheduled_slot)
        WHERE trigger_kind='scheduled';
CREATE INDEX change_registry_observer_jobs_by_scope_time
        ON change_registry_observer_jobs(seller_id,account_scope,requested_at,job_id);
CREATE INDEX change_registry_observer_job_events_by_job
        ON change_registry_observer_job_events(job_id,sequence_no,job_event_id);
CREATE INDEX change_registry_observer_health_by_scope_time
        ON change_registry_observer_health_events(
            seller_id,account_scope,occurred_at,health_event_id
        );
CREATE UNIQUE INDEX change_registry_manual_pending_native_key
        ON change_registry_manual_pending_events(change_item_id,native_event_key)
        WHERE native_event_key<>'';
CREATE INDEX change_registry_manual_pending_events_by_item
        ON change_registry_manual_pending_events(
            change_item_id,occurred_at,pending_id,sequence_no
        );
CREATE TRIGGER change_registry_attempt_identity_consistent
        BEFORE INSERT ON change_registry_attempt_events
        WHEN EXISTS(
            SELECT 1 FROM change_registry_attempt_events
            WHERE attempt_id=NEW.attempt_id AND change_item_id<>NEW.change_item_id
        )
        BEGIN
            SELECT RAISE(ABORT,'change registry attempt identity conflict');
        END;
CREATE TRIGGER change_registry_attempt_lifecycle
        BEFORE INSERT ON change_registry_attempt_events
        BEGIN
            SELECT CASE WHEN NEW.sequence_no=1 AND NEW.state<>'created'
                THEN RAISE(ABORT,'attempt lifecycle must begin with created') END;
            SELECT CASE WHEN NEW.sequence_no>1 AND NOT EXISTS(
                SELECT 1 FROM change_registry_attempt_events previous
                WHERE previous.attempt_id=NEW.attempt_id
                  AND previous.sequence_no=NEW.sequence_no-1
                  AND (
                    (previous.state='created' AND NEW.state IN
                        ('submitted','failed','rejected','cancelled','ambiguous'))
                    OR (previous.state='submitted' AND NEW.state IN
                        ('confirmed','failed','rejected','cancelled','ambiguous'))
                    OR (previous.state='ambiguous' AND NEW.state='resolved')
                  )
                  AND julianday(previous.occurred_at)<=julianday(NEW.occurred_at)
            ) THEN RAISE(ABORT,'attempt lifecycle transition mismatch') END;
        END;
CREATE TRIGGER change_registry_checkpoint_previous_complete
        BEFORE INSERT ON change_registry_checkpoints
        WHEN NEW.previous_complete_checkpoint_id IS NOT NULL AND NOT EXISTS(
            SELECT 1 FROM change_registry_checkpoints previous
            WHERE previous.checkpoint_id=NEW.previous_complete_checkpoint_id
              AND previous.completeness_status='complete'
              AND previous.seller_id=NEW.seller_id
              AND previous.account_scope=NEW.account_scope
              AND julianday(previous.completed_at)<=julianday(NEW.completed_at)
        )
        BEGIN
            SELECT RAISE(ABORT,'previous checkpoint is not a complete same-scope baseline');
        END;
CREATE TRIGGER change_registry_identity_incident_candidates
        BEFORE INSERT ON change_registry_identity_incidents
        WHEN EXISTS(
            SELECT 1 FROM json_each(NEW.candidate_nm_ids_json)
            WHERE type<>'integer' OR value<=0
        ) OR (
            SELECT COUNT(*) FROM json_each(NEW.candidate_nm_ids_json)
        )<>(
            SELECT COUNT(DISTINCT value) FROM json_each(NEW.candidate_nm_ids_json)
        )
        BEGIN
            SELECT RAISE(ABORT,'identity incident candidates must be unique positive integers');
        END;
CREATE TRIGGER change_registry_annotation_parent_chain
        BEFORE INSERT ON change_registry_annotation_revisions
        BEGIN
            SELECT CASE WHEN NEW.parent_revision_id IS NULL AND NEW.revision_no<>1
                THEN RAISE(ABORT,'annotation root revision must be one') END;
            SELECT CASE WHEN NEW.parent_revision_id IS NOT NULL AND NOT EXISTS(
                SELECT 1 FROM change_registry_annotation_revisions parent
                WHERE parent.annotation_revision_id=NEW.parent_revision_id
                  AND parent.subject_kind=NEW.subject_kind
                  AND parent.subject_id=NEW.subject_id
                  AND parent.revision_no+1=NEW.revision_no
                  AND julianday(parent.created_at)<=julianday(NEW.created_at)
            ) THEN RAISE(ABORT,'annotation parent chain mismatch') END;
        END;
CREATE TRIGGER change_registry_manual_pending_lifecycle
        BEFORE INSERT ON change_registry_manual_pending_events
        BEGIN
            SELECT CASE WHEN NEW.sequence_no=1 AND NEW.state<>'pending'
                THEN RAISE(ABORT,'manual pending lifecycle must begin with pending') END;
            SELECT CASE WHEN NEW.sequence_no>1 AND NOT EXISTS(
                SELECT 1 FROM change_registry_manual_pending_events previous
                WHERE previous.pending_id=NEW.pending_id
                  AND previous.change_item_id=NEW.change_item_id
                  AND previous.sequence_no=NEW.sequence_no-1
                  AND previous.state='pending'
                  AND NEW.state IN ('superseded','matched','deviated','expired')
                  AND julianday(previous.occurred_at)<=julianday(NEW.occurred_at)
            ) THEN RAISE(ABORT,'manual pending lifecycle transition mismatch') END;
            SELECT CASE WHEN NEW.related_fact_id IS NOT NULL AND NOT EXISTS(
                SELECT 1
                FROM change_registry_facts fact
                JOIN change_registry_items item ON item.change_item_id=NEW.change_item_id
                WHERE fact.fact_id=NEW.related_fact_id
                  AND fact.seller_id=item.seller_id
                  AND fact.account_scope=item.account_scope
                  AND fact.target_kind=item.target_kind
                  AND fact.nm_id=item.nm_id
                  AND fact.advert_id=item.advert_id
                  AND fact.placement=item.placement
                  AND fact.parameter_field=item.parameter_field
            ) THEN RAISE(ABORT,'manual pending fact target identity mismatch') END;
        END;
CREATE TRIGGER change_registry_manual_current_exact_insert
        BEFORE INSERT ON change_registry_manual_pending_current
        WHEN NOT EXISTS(
            SELECT 1
            FROM change_registry_manual_pending_events event
            JOIN change_registry_items item ON item.change_item_id=event.change_item_id
            WHERE event.pending_event_id=NEW.current_event_id
              AND event.pending_id=NEW.current_pending_id
              AND item.seller_id=NEW.seller_id
              AND item.account_scope=NEW.account_scope
              AND item.target_kind=NEW.target_kind
              AND item.nm_id=NEW.nm_id
              AND item.advert_id=NEW.advert_id
              AND item.placement=NEW.placement
              AND item.parameter_field=NEW.parameter_field
              AND event.occurred_at=NEW.updated_at
              AND NOT EXISTS(
                SELECT 1 FROM change_registry_manual_pending_events later
                WHERE later.pending_id=event.pending_id
                  AND later.sequence_no>event.sequence_no
              )
              AND ((NEW.active=1 AND event.state='pending')
                OR (NEW.active=0 AND event.state IN
                    ('superseded','matched','deviated','expired')))
        )
        BEGIN
            SELECT RAISE(ABORT,'manual pending coordination event mismatch');
        END;
CREATE TRIGGER change_registry_manual_current_exact_update
        BEFORE UPDATE ON change_registry_manual_pending_current
        WHEN NOT EXISTS(
            SELECT 1
            FROM change_registry_manual_pending_events event
            JOIN change_registry_items item ON item.change_item_id=event.change_item_id
            WHERE event.pending_event_id=NEW.current_event_id
              AND event.pending_id=NEW.current_pending_id
              AND item.seller_id=NEW.seller_id
              AND item.account_scope=NEW.account_scope
              AND item.target_kind=NEW.target_kind
              AND item.nm_id=NEW.nm_id
              AND item.advert_id=NEW.advert_id
              AND item.placement=NEW.placement
              AND item.parameter_field=NEW.parameter_field
              AND event.occurred_at=NEW.updated_at
              AND NOT EXISTS(
                SELECT 1 FROM change_registry_manual_pending_events later
                WHERE later.pending_id=event.pending_id
                  AND later.sequence_no>event.sequence_no
              )
              AND ((NEW.active=1 AND event.state='pending')
                OR (NEW.active=0 AND event.state IN
                    ('superseded','matched','deviated','expired')))
        )
        BEGIN
            SELECT RAISE(ABORT,'manual pending coordination event mismatch');
        END;
CREATE TRIGGER change_registry_manual_current_stable_identity
        BEFORE UPDATE ON change_registry_manual_pending_current
        WHEN NEW.seller_id<>OLD.seller_id
          OR NEW.account_scope<>OLD.account_scope
          OR NEW.target_kind<>OLD.target_kind
          OR NEW.nm_id<>OLD.nm_id
          OR NEW.advert_id<>OLD.advert_id
          OR NEW.placement<>OLD.placement
          OR NEW.parameter_field<>OLD.parameter_field
          OR NEW.revision<>OLD.revision+1
          OR julianday(NEW.updated_at)<julianday(OLD.updated_at)
        BEGIN
            SELECT RAISE(ABORT,'manual pending coordination CAS mismatch');
        END;
CREATE TRIGGER change_registry_manual_current_no_delete
        BEFORE DELETE ON change_registry_manual_pending_current
        BEGIN
            SELECT RAISE(ABORT,'manual pending coordination rows are retained');
        END;
CREATE TRIGGER change_registry_observer_lease_cas
        BEFORE UPDATE ON change_registry_observer_leases
        WHEN NEW.seller_id<>OLD.seller_id
          OR NEW.account_scope<>OLD.account_scope
          OR NEW.revision<>OLD.revision+1
          OR julianday(NEW.updated_at)<julianday(OLD.updated_at)
        BEGIN
            SELECT RAISE(ABORT,'change registry observer lease CAS mismatch');
        END;
CREATE TRIGGER change_registry_observer_lease_no_delete
        BEFORE DELETE ON change_registry_observer_leases
        BEGIN
            SELECT RAISE(ABORT,'change registry observer lease rows are retained');
        END;
CREATE TRIGGER change_registry_operations_no_update
            BEFORE UPDATE ON change_registry_operations
            BEGIN
                SELECT RAISE(ABORT,'change registry canonical row is immutable');
            END;
CREATE TRIGGER change_registry_operations_no_delete
            BEFORE DELETE ON change_registry_operations
            BEGIN
                SELECT RAISE(ABORT,'change registry canonical row is append-only');
            END;
CREATE TRIGGER change_registry_items_no_update
            BEFORE UPDATE ON change_registry_items
            BEGIN
                SELECT RAISE(ABORT,'change registry canonical row is immutable');
            END;
CREATE TRIGGER change_registry_items_no_delete
            BEFORE DELETE ON change_registry_items
            BEGIN
                SELECT RAISE(ABORT,'change registry canonical row is append-only');
            END;
CREATE TRIGGER change_registry_attempt_events_no_update
            BEFORE UPDATE ON change_registry_attempt_events
            BEGIN
                SELECT RAISE(ABORT,'change registry canonical row is immutable');
            END;
CREATE TRIGGER change_registry_attempt_events_no_delete
            BEFORE DELETE ON change_registry_attempt_events
            BEGIN
                SELECT RAISE(ABORT,'change registry canonical row is append-only');
            END;
CREATE TRIGGER change_registry_facts_no_update
            BEFORE UPDATE ON change_registry_facts
            BEGIN
                SELECT RAISE(ABORT,'change registry canonical row is immutable');
            END;
CREATE TRIGGER change_registry_facts_no_delete
            BEFORE DELETE ON change_registry_facts
            BEGIN
                SELECT RAISE(ABORT,'change registry canonical row is append-only');
            END;
CREATE TRIGGER change_registry_fact_links_no_update
            BEFORE UPDATE ON change_registry_fact_links
            BEGIN
                SELECT RAISE(ABORT,'change registry canonical row is immutable');
            END;
CREATE TRIGGER change_registry_fact_links_no_delete
            BEFORE DELETE ON change_registry_fact_links
            BEGIN
                SELECT RAISE(ABORT,'change registry canonical row is append-only');
            END;
CREATE TRIGGER change_registry_checkpoints_no_update
            BEFORE UPDATE ON change_registry_checkpoints
            BEGIN
                SELECT RAISE(ABORT,'change registry canonical row is immutable');
            END;
CREATE TRIGGER change_registry_checkpoints_no_delete
            BEFORE DELETE ON change_registry_checkpoints
            BEGIN
                SELECT RAISE(ABORT,'change registry canonical row is append-only');
            END;
CREATE TRIGGER change_registry_observation_values_no_update
            BEFORE UPDATE ON change_registry_observation_values
            BEGIN
                SELECT RAISE(ABORT,'change registry canonical row is immutable');
            END;
CREATE TRIGGER change_registry_observation_values_no_delete
            BEFORE DELETE ON change_registry_observation_values
            BEGIN
                SELECT RAISE(ABORT,'change registry canonical row is append-only');
            END;
CREATE TRIGGER change_registry_identity_incidents_no_update
            BEFORE UPDATE ON change_registry_identity_incidents
            BEGIN
                SELECT RAISE(ABORT,'change registry canonical row is immutable');
            END;
CREATE TRIGGER change_registry_identity_incidents_no_delete
            BEFORE DELETE ON change_registry_identity_incidents
            BEGIN
                SELECT RAISE(ABORT,'change registry canonical row is append-only');
            END;
CREATE TRIGGER change_registry_annotation_revisions_no_update
            BEFORE UPDATE ON change_registry_annotation_revisions
            BEGIN
                SELECT RAISE(ABORT,'change registry canonical row is immutable');
            END;
CREATE TRIGGER change_registry_annotation_revisions_no_delete
            BEFORE DELETE ON change_registry_annotation_revisions
            BEGIN
                SELECT RAISE(ABORT,'change registry canonical row is append-only');
            END;
CREATE TRIGGER change_registry_manual_pending_events_no_update
            BEFORE UPDATE ON change_registry_manual_pending_events
            BEGIN
                SELECT RAISE(ABORT,'change registry canonical row is immutable');
            END;
CREATE TRIGGER change_registry_manual_pending_events_no_delete
            BEFORE DELETE ON change_registry_manual_pending_events
            BEGIN
                SELECT RAISE(ABORT,'change registry canonical row is append-only');
            END;
CREATE TRIGGER change_registry_checkpoint_source_manifests_no_update
            BEFORE UPDATE ON change_registry_checkpoint_source_manifests
            BEGIN
                SELECT RAISE(ABORT,'change registry canonical row is immutable');
            END;
CREATE TRIGGER change_registry_checkpoint_source_manifests_no_delete
            BEFORE DELETE ON change_registry_checkpoint_source_manifests
            BEGIN
                SELECT RAISE(ABORT,'change registry canonical row is append-only');
            END;
CREATE TRIGGER change_registry_observer_jobs_no_update
            BEFORE UPDATE ON change_registry_observer_jobs
            BEGIN
                SELECT RAISE(ABORT,'change registry canonical row is immutable');
            END;
CREATE TRIGGER change_registry_observer_jobs_no_delete
            BEFORE DELETE ON change_registry_observer_jobs
            BEGIN
                SELECT RAISE(ABORT,'change registry canonical row is append-only');
            END;
CREATE TRIGGER change_registry_observer_job_events_no_update
            BEFORE UPDATE ON change_registry_observer_job_events
            BEGIN
                SELECT RAISE(ABORT,'change registry canonical row is immutable');
            END;
CREATE TRIGGER change_registry_observer_job_events_no_delete
            BEFORE DELETE ON change_registry_observer_job_events
            BEGIN
                SELECT RAISE(ABORT,'change registry canonical row is append-only');
            END;
CREATE TRIGGER change_registry_observer_health_events_no_update
            BEFORE UPDATE ON change_registry_observer_health_events
            BEGIN
                SELECT RAISE(ABORT,'change registry canonical row is immutable');
            END;
CREATE TRIGGER change_registry_observer_health_events_no_delete
            BEFORE DELETE ON change_registry_observer_health_events
            BEGIN
                SELECT RAISE(ABORT,'change registry canonical row is append-only');
            END;
COMMIT;
