"""Content quality pattern detection tests."""

from mnemon.search.quality import check_content_quality


class TestInstanceIdDetected:
    """AWS instance IDs trigger warnings."""

    def test_instance_id_detected(self):
        """AWS instance ID pattern matched."""
        w = check_content_quality('Deployed i-0c220c2402a5245bc')
        assert 'AWS instance ID' in w


class TestResourceCountDetected:
    """Resource count language triggers warning."""

    def test_resource_count_detected(self):
        """'N resources total' pattern matched."""
        w = check_content_quality('32 resources total in the stack')
        assert 'resource count' in w

    def test_singular_resource(self):
        """'1 resource total' also matched."""
        w = check_content_quality('1 resource total')
        assert 'resource count' in w


class TestVerificationReceipt:
    """Verification language triggers warning."""

    def test_all_verified(self):
        """'All drives verified' pattern matched."""
        w = check_content_quality('All drives verified: D: 2500GB')
        assert 'verification receipt' in w

    def test_every_verified(self):
        """'every ... verified' variant matched."""
        w = check_content_quality('Every instance verified healthy')
        assert 'verification receipt' in w


class TestStateObservation:
    """State observation language triggers warning."""

    def test_state_clean(self):
        """'state clean' pattern matched."""
        w = check_content_quality('Terraform state clean after apply')
        assert 'state observation' in w

    def test_state_is_clean(self):
        """'state is clean' variant matched."""
        w = check_content_quality('State is clean')
        assert 'state observation' in w


class TestDeploymentReceipt:
    """Deployment receipt language triggers warning."""

    def test_deployed_via(self):
        """'deployed via' pattern matched."""
        w = check_content_quality('Stack deployed via Terraform')
        assert 'deployment receipt' in w

    def test_applied_via(self):
        """'applied via' variant matched."""
        w = check_content_quality('Changes applied via CI pipeline')
        assert 'deployment receipt' in w


class TestLineNumberReference:
    """Line number references trigger warnings."""

    def test_line_number(self):
        """'line 42' pattern matched."""
        w = check_content_quality('Error on line 42 of the module')
        assert 'line number reference' in w

    def test_line_number_case_insensitive(self):
        """'Line 100' case-insensitive match."""
        w = check_content_quality('Line 100 has the bug')
        assert 'line number reference' in w


class TestLineCount:
    """Line count references trigger warnings."""

    def test_line_count(self):
        """'4841 lines' pattern matched."""
        w = check_content_quality('The file grew to 4841 lines')
        assert 'line count' in w

    def test_two_digit_line_count(self):
        """'50 lines' also matched."""
        w = check_content_quality('Function is 50 lines long')
        assert 'line count' in w

    def test_single_digit_no_match(self):
        """'3 lines' should not match (single digit)."""
        w = check_content_quality('Only 3 lines of config')
        assert 'line count' not in w


class TestSymbolLineReference:
    """Function:line-number references trigger warnings."""

    def test_function_line_ref(self):
        """'main:28' pattern matched."""
        w = check_content_quality('See main:28 for the entry point')
        assert 'function/symbol line reference' in w

    def test_long_symbol_ref(self):
        """'import_issuer_data:121' pattern matched."""
        w = check_content_quality(
            'Fixed import_issuer_data:121 off-by-one')
        assert 'function/symbol line reference' in w

    def test_single_digit_no_match(self):
        """'port:5' should not match (single digit)."""
        w = check_content_quality('Set port:5 for debugging')
        assert 'function/symbol line reference' not in w


class TestLineNumberCorrection:
    """Arrow-style line corrections trigger warnings."""

    def test_arrow_correction(self):
        """'422→421' pattern matched."""
        w = check_content_quality('Line changed 422→421 after edit')
        assert 'line number correction' in w


class TestCleanContentNoWarnings:
    """Durable reasoning produces no warnings."""

    def test_durable_fact(self):
        """Platform behavior insight triggers nothing."""
        w = check_content_quality(
            'EC2Launch v2 does not re-run userdata by default')
        assert w == []

    def test_architectural_decision(self):
        """Design decision triggers nothing."""
        w = check_content_quality(
            'Chose SQLite over Postgres for single-node simplicity')
        assert w == []

    def test_user_preference(self):
        """User preference triggers nothing."""
        w = check_content_quality(
            'User prefers snake_case for all variable names')
        assert w == []


class TestNoFalsePositives:
    """Good entries from tradar DB produce zero warnings."""

    def test_ebsnvme_entry(self):
        """ebsnvme-id /dev/ prefix entry — no warnings."""
        w = check_content_quality(
            'ebsnvme-id outputs device paths with /dev/ prefix')
        assert w == []

    def test_rds_sa_entry(self):
        """RDS sa not sysadmin entry — no warnings."""
        w = check_content_quality(
            'Cannot grant sysadmin to sa in RDS SQL Server')
        assert w == []

    def test_quicksetup_ssm(self):
        """QuickSetup SSM entry — no warnings."""
        w = check_content_quality(
            'QuickSetup SSM duplicates via CloudFormation stacks')
        assert w == []

    def test_port_number_no_false_positive(self):
        """'port:5432' should not trigger symbol line reference."""
        w = check_content_quality('Connect to port:5 for the service')
        assert 'function/symbol line reference' not in w


class TestMultipleWarnings:
    """Content with multiple transient patterns returns all warnings."""

    def test_multiple_patterns(self):
        """Deployment receipt with instance IDs triggers multiple warnings."""
        content = (
            'TC-DB-01 (i-0c220c2402a5245bc) deployed via Terraform.'
            ' 32 resources total. All drives verified. State is clean.')
        w = check_content_quality(content)
        assert 'AWS instance ID' in w
        assert 'resource count' in w
        assert 'verification receipt' in w
        assert 'state observation' in w
        assert 'deployment receipt' in w
        assert len(w) == 5
