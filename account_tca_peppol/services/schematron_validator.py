# Part of TCA. See LICENSE file for full copyright and licensing details.
"""
Schematron validation for PINT AE XML using saxonche (Saxon C/Python binding).

Validates generated XML against the official PINT AE schematron rules:
  1. PINT-UBL-validation-preprocessed.xslt — base PINT rules
  2. PINT-jurisdiction-aligned-rules.xslt — UAE-specific rules

Soft dependency: if saxonche is not installed, validation is skipped gracefully.
Install with: pip install saxonche
"""

import logging
import os
import tempfile
import threading

from odoo import api, models

_logger = logging.getLogger(__name__)

try:
    from saxonche import PySaxonProcessor
    SAXONCHE_AVAILABLE = True
except ImportError:
    SAXONCHE_AVAILABLE = False
    _logger.info('saxonche not installed — schematron validation disabled. Install with: pip install saxonche')

# Schematron XSLT paths relative to the PINT AE resources
_SCHEMATRON_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'data', 'schematron')

# ── PySaxonProcessor singleton ────────────────────────────────────────────────
# Saxon-C processor initialization spins up a native (GraalVM-based) runtime
# which is expensive — multiple hundreds of ms per construction. Per the
# saxonche docs, the processor is designed to be a long-lived singleton; only
# the XsltExecutable should be created per transformation. We keep one
# processor per Odoo worker process, lazily initialized under a lock so the
# first concurrent calls don't double-construct.
_processor = None
_processor_lock = threading.Lock()


def _get_saxon_processor():
    """Return the module-level PySaxonProcessor singleton.
    Lazy-initialized on first call; thread-safe via double-checked locking."""
    global _processor  # noqa: PLW0603 — module-level lazy singleton (per-worker)
    if _processor is None:
        with _processor_lock:
            if _processor is None:
                _processor = PySaxonProcessor(license=False)
    return _processor


class TcaSchematronValidator(models.AbstractModel):
    _name = 'tca.schematron.validator'
    _description = 'PINT AE Schematron Validator'

    @api.model
    def is_available(self):
        """Check if schematron validation is available (saxonche installed + XSLT files present)."""
        if not SAXONCHE_AVAILABLE:
            return False
        return os.path.isdir(_SCHEMATRON_DIR) and any(
            f.endswith('.xslt') for f in os.listdir(_SCHEMATRON_DIR)
        )

    @api.model
    def validate_xml(self, xml_bytes, is_credit_note=False):
        """
        Validate PINT AE XML against official schematron rules.

        Args:
            xml_bytes: Raw XML bytes to validate
            is_credit_note: If True, uses credit note schematron (same rules for PINT AE)

        Returns:
            dict with:
                'valid': bool — True if no fatal errors
                'fatal_errors': list of {'rule_id': str, 'message': str}
                'warnings': list of {'rule_id': str, 'message': str}
                'skipped': bool — True if validation was skipped (saxonche not available)
        """
        if not SAXONCHE_AVAILABLE:
            return {'valid': True, 'fatal_errors': [], 'warnings': [], 'skipped': True}

        xslt_files = self._get_xslt_files()
        if not xslt_files:
            _logger.warning('TCA schematron: no XSLT files found in %s', _SCHEMATRON_DIR)
            return {'valid': True, 'fatal_errors': [], 'warnings': [], 'skipped': True}

        all_fatal = []
        all_warnings = []

        for xslt_name, xslt_path in xslt_files:
            try:
                errors = self._run_schematron(xml_bytes, xslt_path)
                for err in errors:
                    if err['severity'] == 'fatal':
                        all_fatal.append(err)
                    else:
                        all_warnings.append(err)
            except Exception as exc:
                _logger.error('TCA schematron: failed to run %s: %s', xslt_name, exc)

        return {
            'valid': len(all_fatal) == 0,
            'fatal_errors': all_fatal,
            'warnings': all_warnings,
            'skipped': False,
        }

    @api.model
    def _get_xslt_files(self):
        """Return list of (name, path) tuples for available schematron XSLT files."""
        if not os.path.isdir(_SCHEMATRON_DIR):
            return []
        files = []
        for f in sorted(os.listdir(_SCHEMATRON_DIR)):
            if f.endswith('.xslt'):
                files.append((f, os.path.join(_SCHEMATRON_DIR, f)))
        return files

    @api.model
    def _run_schematron(self, xml_bytes, xslt_path):
        """
        Run a single schematron XSLT against XML bytes.
        Returns list of {'rule_id', 'severity', 'message'}.

        The temp XML lives inside a TemporaryDirectory context manager so it
        is always cleaned up — even if XSLT processing or SVRL parsing raises.
        The Saxon processor is reused across calls (see _get_saxon_processor).
        """
        from lxml import etree

        # saxonche needs a file path for the input XML — keep it scoped to a
        # temp directory that auto-cleans on context exit, regardless of how
        # the block ends.
        with tempfile.TemporaryDirectory(prefix='tca_sch_') as tmp_dir:
            tmp_path = os.path.join(tmp_dir, 'doc.xml')
            with open(tmp_path, 'wb') as tmp_fh:
                tmp_fh.write(
                    xml_bytes if isinstance(xml_bytes, bytes) else xml_bytes.encode()
                )

            proc = _get_saxon_processor()
            xslt = proc.new_xslt30_processor()
            xslt.set_cwd('/')
            result = xslt.transform_to_string(
                source_file=tmp_path, stylesheet_file=xslt_path,
            )

            if not result:
                return []

            # Parse SVRL output
            svrl = etree.fromstring(result.encode())
            ns = {'svrl': 'http://purl.oclc.org/dsdl/svrl'}
            failed = svrl.xpath('//svrl:failed-assert', namespaces=ns)

            errors = []
            for f in failed:
                text = (f.findtext('svrl:text', namespaces=ns, default='') or '').strip()
                errors.append({
                    'rule_id': f.get('id', ''),
                    'severity': f.get('flag', 'fatal'),
                    'message': text,
                })
            return errors
