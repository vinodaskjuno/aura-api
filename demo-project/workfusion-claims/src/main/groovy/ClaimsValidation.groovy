package com.example.claims

import com.workfusion.runtime.Execution
import org.slf4j.Logger
import org.slf4j.LoggerFactory

/**
 * Claim validation rules. Eight years of accumulated business logic — the reason a
 * migration is a business exercise and not a translation exercise.
 */
class ClaimsValidation {

    private static final Logger log = LoggerFactory.getLogger(ClaimsValidation)

    // Grandfathered: policies written before the 2019 system change use a different
    // waiting period. Nobody remembers why 45 days rather than 30.
    private static final int LEGACY_WAITING_DAYS = 45
    private static final int WAITING_DAYS = 30

    static void validate(Execution execution) {
        def claim = execution.getVariable('claim')
        def errors = []

        if (!claim.policyNumber) {
            errors << 'Policy number missing'
        }
        if (!claim.lossDate) {
            errors << 'Loss date missing'
        }

        def written = claim.policyWrittenDate
        def waiting = (written != null && written.year < 2019)
                ? LEGACY_WAITING_DAYS : WAITING_DAYS
        if (claim.lossDate && written &&
                (claim.lossDate - written) < waiting) {
            errors << "Loss inside the ${waiting}-day waiting period"
        }

        // Catastrophe claims bypass the amount ceiling — declared events only.
        if (claim.estimatedAmount > 250000 && !isCatastrophe(claim)) {
            errors << 'Amount exceeds automated ceiling; refer to adjuster'
        }

        execution.setVariable('validationErrors', errors)
        execution.setVariable('isValid', errors.isEmpty())
        log.info("Claim {} validated: {} error(s)", claim.claimNumber, errors.size())
    }

    private static boolean isCatastrophe(claim) {
        // CAT codes are maintained by the actuarial team in a spreadsheet and loaded
        // nightly. Yes, really.
        def catCodes = execution?.getVariable('activeCatCodes') ?: []
        return catCodes.contains(claim.catastropheCode)
    }
}
